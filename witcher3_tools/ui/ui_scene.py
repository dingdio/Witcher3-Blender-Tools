
import logging
import os
from datetime import datetime
from pathlib import Path
log = logging.getLogger(__name__)

from ..CR2W import w3_types
from ..CR2W.prop_utils import prop_to_string
from ..CR2W.common_blender import get_repo_override_state, repo_file, set_repo_override_roots
from .. import dialog_language
from ..importers import import_cutscene
from ..importers import import_scene
from .. import setup_logging_bl
from ..extension_paths import get_cache_root

import bpy
from bpy.types import Panel, Operator, UIList, PropertyGroup
from bpy.props import IntProperty, StringProperty, CollectionProperty, FloatProperty, BoolProperty
from bpy_extras.io_utils import ImportHelper

from .. import get_all_addon_prefs, get_uncook_path
from .ui_cutscene import (
    _clear_loaded_cutscene_state,
    _cs_find_camera_armature,
    _draw_camera_track_controls,
    _event_type_icon,
    _get_loaded_cutscene_actor_object,
    _load_cutscene_dialogs_into_scene,
    _same_filesystem_path,
    _sync_loaded_cutscene_state,
)
from .ui_cr2w_fields import (
    _draw_imported_class_sections,
    _format_imported_field_value,
    _get_imported_field_value,
    _get_present_imported_fields,
)
from .ui_utils import WITCH_PT_Base
from ..camera_tracks import (
    CAMERA_CONTROL_BONE,
)

from ..CR2W.scene_csv_utils import (
    _parse_body_anim_csv,
    _parse_mimics_csv,
    _lookup_dialogset_body_anim,
    _resolve_mimic_layer_anim,
    _resolve_mimic_layer_anim_candidates,
)


_DIALOGSET_IDLE_TRACK = "SceneDialogsetIdle"
_W2SCENE_IMPORT_PROFILE_LOG_FORMAT = "%(asctime)s %(levelname)8s %(name)s %(message)s"
_W2SCENE_IMPORT_PROFILE_LOG_DATEFMT = "%H:%M:%S"
_DIALOGSET_STATUS_ITEM_CACHE = []
_DIALOGSET_EMOTIONAL_ITEM_CACHE = {}
_DIALOGSET_POSE_ITEM_CACHE = {}
_DIALOGSET_MIMICS_STATE_ITEM_CACHE = []
_DIALOGSET_SYNCING_SLOT_PROPS = False


class _W2SceneImportProfileLogFormatter(logging.Formatter):
    def format(self, record):
        original_name = record.name
        display_name = getattr(setup_logging_bl, "_display_logger_name", None)
        if callable(display_name):
            try:
                record.name = display_name(original_name)
            except Exception:
                record.name = original_name
        try:
            return super().format(record)
        finally:
            record.name = original_name


def _new_w2scene_import_profile_job_state():
    return {
        "profile_log_path": "",
        "profile_log_handler": None,
        "profile_log_level_changes": [],
    }


def _w2scene_import_profile_logger():
    addon_name = str(getattr(setup_logging_bl, "ADDON_NAME", "") or "").strip()
    if addon_name:
        return logging.getLogger(addon_name)
    return logging.getLogger(__name__.split(".", 1)[0])


def _w2scene_import_profile_logger_names():
    root_name = _w2scene_import_profile_logger().name or __name__.split(".", 1)[0]
    return (
        root_name,
        f"{root_name}.ui.ui_scene",
        f"{root_name}.ui.ui_anims",
        f"{root_name}.ui.ui_anims_list",
        f"{root_name}.importers.import_scene",
        f"{root_name}.importers.import_scene_animation",
        f"{root_name}.importers.import_anims",
        f"{root_name}.importers.import_cutscene",
        f"{root_name}.CR2W.CR2W_types",
    )


def _enable_w2scene_import_profile_loggers(job):
    changes = []
    for logger_name in _w2scene_import_profile_logger_names():
        logger_obj = logging.getLogger(logger_name)
        previous_level = logger_obj.level
        if previous_level == logging.NOTSET or previous_level > logging.INFO:
            logger_obj.setLevel(logging.INFO)
        changes.append((logger_obj, previous_level))
    job["profile_log_level_changes"] = changes


def _restore_w2scene_import_profile_loggers(job):
    changes = list((job or {}).get("profile_log_level_changes", []) or [])
    for logger_obj, previous_level in reversed(changes):
        try:
            logger_obj.setLevel(previous_level)
        except Exception:
            pass
    if job is not None:
        job["profile_log_level_changes"] = []


def _w2scene_import_profile_log_root():
    log_dir = Path(get_cache_root(create=True)) / "scene_import_profile_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def _sanitize_w2scene_profile_label(value):
    text = str(value or "").strip()
    if not text:
        return "scene"
    sanitized = "".join(ch if ch.isalnum() else "_" for ch in text)
    sanitized = sanitized.strip("_")
    while "__" in sanitized:
        sanitized = sanitized.replace("__", "_")
    return sanitized[:80] or "scene"


def _create_w2scene_import_profile_log_path(scene_path="", section_name=""):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    scene_label = _sanitize_w2scene_profile_label(Path(str(scene_path or "scene")).stem)
    section_label = _sanitize_w2scene_profile_label(section_name or "section")
    filename = f"scene_import_profile_log_{scene_label}_{section_label}_{timestamp}.txt"
    return _w2scene_import_profile_log_root() / filename


def _start_w2scene_import_profile_log(job, scene_path="", section_name=""):
    if job is None:
        return ""
    if job.get("profile_log_handler") is not None:
        return str(job.get("profile_log_path", "") or "")
    try:
        log_path = _create_w2scene_import_profile_log_path(scene_path, section_name)
        handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(_W2SceneImportProfileLogFormatter(_W2SCENE_IMPORT_PROFILE_LOG_FORMAT, datefmt=_W2SCENE_IMPORT_PROFILE_LOG_DATEFMT))
        _w2scene_import_profile_logger().addHandler(handler)
        _enable_w2scene_import_profile_loggers(job)
    except Exception:
        log.exception("Failed to start .w2scene import profile log")
        return ""
    job["profile_log_path"] = str(log_path)
    job["profile_log_handler"] = handler
    log.info("Writing .w2scene import profile log to %s", log_path)
    return str(log_path)


def _stop_w2scene_import_profile_log(job, completion_message=""):
    if job is None:
        return ""
    handler = job.get("profile_log_handler")
    log_path = str(job.get("profile_log_path", "") or "")
    if handler is None:
        return log_path
    try:
        if completion_message:
            log.info("%s", completion_message)
    finally:
        try:
            _w2scene_import_profile_logger().removeHandler(handler)
        except Exception:
            pass
        try:
            handler.close()
        except Exception:
            pass
        job["profile_log_handler"] = None
        _restore_w2scene_import_profile_loggers(job)
    return log_path


def _dialogset_status_items(self, context):
    global _DIALOGSET_STATUS_ITEM_CACHE
    if _DIALOGSET_STATUS_ITEM_CACHE:
        return _DIALOGSET_STATUS_ITEM_CACHE
    data = _parse_body_anim_csv()
    seen = set()
    for (s, _e, _p), v in data.items():
        seen.add(v.get("status_display", s))
    _DIALOGSET_STATUS_ITEM_CACHE = [(d, d, "") for d in sorted(seen)] or [("High", "High", "")]
    return _DIALOGSET_STATUS_ITEM_CACHE


def _dialogset_emotional_items(self, context):
    data = _parse_body_anim_csv()
    wm = getattr(context, "window_manager", None) if context else None
    sk = str(getattr(wm, "witcher_dialogset_status", "") or "").strip().lower()
    if sk in _DIALOGSET_EMOTIONAL_ITEM_CACHE:
        return _DIALOGSET_EMOTIONAL_ITEM_CACHE[sk]
    seen = set()
    for (s, e, _p), v in data.items():
        if not sk or s == sk:
            seen.add(v.get("emotional_display", e))
    items = [(d, d, "") for d in sorted(seen)] or [("Determined", "Determined", "")]
    _DIALOGSET_EMOTIONAL_ITEM_CACHE[sk] = items
    return items


def _dialogset_pose_items(self, context):
    data = _parse_body_anim_csv()
    wm = getattr(context, "window_manager", None) if context else None
    sk = str(getattr(wm, "witcher_dialogset_status", "") or "").strip().lower()
    ek = str(getattr(wm, "witcher_dialogset_emotional_state", "") or "").strip().lower()
    cache_key = (sk, ek)
    if cache_key in _DIALOGSET_POSE_ITEM_CACHE:
        return _DIALOGSET_POSE_ITEM_CACHE[cache_key]
    seen = set()
    for (s, e, _p), v in data.items():
        if (not sk or s == sk) and (not ek or e == ek) and v["idles"]:
            seen.add(v.get("pose_display", _p))
    items = [(d, d, "") for d in sorted(seen)] or [("Standing", "Standing", "")]
    _DIALOGSET_POSE_ITEM_CACHE[cache_key] = items
    return items


def _dialogset_mimics_state_items(self, context):
    global _DIALOGSET_MIMICS_STATE_ITEM_CACHE
    if _DIALOGSET_MIMICS_STATE_ITEM_CACHE:
        return _DIALOGSET_MIMICS_STATE_ITEM_CACHE
    data = _parse_mimics_csv()
    items = [("None", "None", "(no preset; per-layer values used)")]
    items.extend((v["display"], v["display"], "") for v in data.values())
    _DIALOGSET_MIMICS_STATE_ITEM_CACHE = items
    return _DIALOGSET_MIMICS_STATE_ITEM_CACHE


_dialogset_mimics_layer_items = _dialogset_mimics_state_items


def _w2scene_find_camera_armature(context):
    camera_arm = _cs_find_camera_armature(context)
    if camera_arm is not None:
        return camera_arm
    scene = getattr(context, "scene", None)
    for obj in getattr(scene, "objects", []) or []:
        if (
            getattr(obj, "type", None) == 'ARMATURE'
            and getattr(getattr(obj, "pose", None), "bones", {}).get(CAMERA_CONTROL_BONE) is not None
        ):
            return obj
    return None


def add_scene_section(name, json_data, scene):
    if not hasattr(scene, "witcher_sections"):
        scene["witcher_sections"] = []
    
    section = scene.witcher_sections.add()
    section.name = name
    section.json_data = json_data
    return section

class WitcherSection(bpy.types.PropertyGroup):
    name: StringProperty(name="Name")
    json_data: StringProperty(name="JSON Data")
    section_index: IntProperty(default=-1)
    section_type: StringProperty(default="")
    section_id: IntProperty(default=0)
    element_count: IntProperty(default=0)
    event_count: IntProperty(default=0)
    duration: FloatProperty(default=0.0)
    dialogset_change: StringProperty(default="")
    linked_cutscene: StringProperty(default="")
    is_gameplay: BoolProperty(default=False)
    is_important: BoolProperty(default=False)

class W2SceneFieldItem(PropertyGroup):
    section_index: IntProperty(default=-1)
    class_name: StringProperty(default="")
    field_name: StringProperty(default="")
    value_text: StringProperty(default="")
    is_set: BoolProperty(default=False)


class W2SceneActorItem(PropertyGroup):
    item_type: StringProperty(default="ACTOR")
    source_index: IntProperty(default=-1)
    actor_id: StringProperty(default="")
    alias: StringProperty(default="")
    actor_tags: StringProperty(default="")
    template_path: StringProperty(default="")
    appearance_filter: StringProperty(default="")
    use_mimic: BoolProperty(default=False)
    force_spawn: BoolProperty(default=False)
    dont_search_by_voicetag: BoolProperty(default=False)
    force_behavior_graph: StringProperty(default="")
    reset_behavior_graph: BoolProperty(default=False)
    light_type: StringProperty(default="")
    shadow_casting_mode: StringProperty(default="")
    inner_angle: FloatProperty(default=0.0)
    outer_angle: FloatProperty(default=0.0)
    softness: FloatProperty(default=0.0)
    dimmer_type: StringProperty(default="")


class W2SceneDialogsetItem(PropertyGroup):
    source_index: IntProperty(default=-1)
    name: StringProperty(default="")
    placement_tag: StringProperty(default="")
    path: StringProperty(default="")
    slot_count: IntProperty(default=0)
    snap_to_terrain: BoolProperty(default=False)
    find_safe_placement: BoolProperty(default=False)


class W2SceneDialogsetSlotItem(PropertyGroup):
    dialogset_index: IntProperty(default=-1)
    source_index: IntProperty(default=-1)
    slot_number: IntProperty(default=0)
    slot_name: StringProperty(default="")
    actor_name: StringProperty(default="")
    actor_status: StringProperty(default="")
    actor_pose_name: StringProperty(default="")
    actor_emotional_state: StringProperty(default="")
    actor_mimics_state: StringProperty(default="")
    force_body_idle_animation: StringProperty(default="")
    actor_visibility: BoolProperty(default=True)
    actor_template_path: StringProperty(default="")
    slot_place_x: FloatProperty(default=0.0)
    slot_place_y: FloatProperty(default=0.0)
    slot_place_z: FloatProperty(default=0.0)
    slot_place_yaw: FloatProperty(default=0.0)
    slot_place_pitch: FloatProperty(default=0.0)
    slot_place_roll: FloatProperty(default=0.0)
    actor_mimics_layer_eyes: StringProperty(default="")
    actor_mimics_layer_pose: StringProperty(default="")
    actor_mimics_layer_animation: StringProperty(default="")
    actor_mimics_layer_pose_weight: FloatProperty(default=1.0)
    force_body_idle_animation_weight: FloatProperty(default=1.0)


class W2SceneCameraItem(PropertyGroup):
    source_index: IntProperty(default=-1)
    camera_name: StringProperty(default="")
    fov: FloatProperty(default=0.0)
    zoom: FloatProperty(default=0.0)
    source_slot_name: StringProperty(default="")
    target_slot_name: StringProperty(default="")
    camera_adjust_version: IntProperty(default=0)
    dof_summary: StringProperty(default="")


class W2SceneSectionElementItem(PropertyGroup):
    section_index: IntProperty(default=-1)
    source_index: IntProperty(default=-1)
    element_type: StringProperty(default="")
    element_id: StringProperty(default="")
    display_name: StringProperty(default="")
    start_time: FloatProperty(default=0.0)
    duration: FloatProperty(default=0.0)
    actor: StringProperty(default="")
    target: StringProperty(default="")
    line_id: StringProperty(default="")
    line_text: StringProperty(default="")
    detail_text: StringProperty(default="")


class W2SceneSectionEventItem(PropertyGroup):
    section_index: IntProperty(default=-1)
    source_index: IntProperty(default=-1)
    event_type: StringProperty(default="")
    event_name: StringProperty(name="Event Name", default="")
    start_time: FloatProperty(name="Start Time", default=0.0)
    start_position: FloatProperty(name="Start Position", default=0.0)
    duration: FloatProperty(name="Duration", default=0.0)
    duration_raw: FloatProperty(name="Raw Duration", default=0.0)
    actor: StringProperty(name="Actor", default="")
    target: StringProperty(name="Target", default="")
    scene_element_id: StringProperty(name="Element", default="")
    track_name: StringProperty(name="Track", default="")
    animation_name: StringProperty(name="Animation", default="")
    camera_name: StringProperty(name="Camera", default="")
    effect_name: StringProperty(name="Effect", default="")
    guid: StringProperty(name="GUID", default="")
    is_muted: BoolProperty(name="Muted", default=False)
    detail_text: StringProperty(default="")


_W2SCENE_ROOT_FIELD_SCHEMA = [
    ("CStoryScene", [
        ("sceneId", None),
        ("elementIDCounter", None),
        ("sectionIDCounter", None),
        ("mayActorsStartWorking", None),
        ("surpassWaterRendering", None),
        ("blockMusicTriggers", None),
        ("muteSpeechUnderWater", None),
        ("soundListenerOverride", None),
        ("banksDependency", None),
        ("soundEventsOnEnd", None),
        ("soundEventsOnSkip", None),
        ("sceneTemplates", None),
        ("sceneProps", None),
        ("sceneEffects", None),
        ("sceneLights", None),
        ("dialogsetInstances", None),
        ("cameraDefinitions", None),
        ("sections", None),
        ("controlParts", None),
    ]),
]

_W2SCENE_SECTION_FIELD_SCHEMA = [
    ("CStorySceneSection", [
        ("sectionId", None),
        ("sectionName", None),
        ("comment", None),
        ("contexID", None),
        ("defaultVariantId", None),
        ("nextVariantId", None),
        ("variants", None),
        ("localeVariantMappings", None),
        ("sceneElements", None),
        ("events", None),
        ("eventsInfo", None),
        ("choice", None),
        ("tags", None),
        ("isGameplay", None),
        ("isImportant", None),
        ("allowCameraMovement", None),
        ("hasCinematicOneliners", None),
        ("manualFadeIn", None),
        ("fadeInAtBeginning", None),
        ("fadeOutAtEnd", None),
        ("pauseInCombat", None),
        ("canBeSkipped", None),
        ("canHaveLookats", None),
        ("dialogsetChangeTo", None),
        ("forceDialogset", None),
        ("streamingLock", None),
        ("streamingAreaTag", None),
        ("blockMusicTriggers", None),
        ("soundListenerOverride", None),
        ("soundEventsOnEnd", None),
        ("soundEventsOnSkip", None),
    ]),
    ("CStorySceneCutsceneSection", [
        ("cutscene", None),
        ("sceneEventElements", None),
        ("isLooped", None),
        ("canBeSkipped", None),
    ]),
    ("CStorySceneVideoSection", [
        ("videoElement", None),
    ]),
]


def _w2scene_clear_loaded_state(scene):
    for prop_name in (
        "witcher_sections",
        "witcher_w2scene_root_fields",
        "witcher_w2scene_section_fields",
        "witcher_w2scene_actor_items",
        "witcher_w2scene_dialogset_items",
        "witcher_w2scene_dialogset_slot_items",
        "witcher_w2scene_camera_items",
        "witcher_w2scene_section_element_items",
        "witcher_w2scene_section_event_items",
    ):
        collection = getattr(scene, prop_name, None)
        if collection is not None:
            collection.clear()
    scene.witcher_loaded_w2scene_name = ""
    scene.witcher_loaded_w2scene_path = ""
    scene.witcher_w2scene_repo_path = ""
    scene.witcher_w2scene_summary = ""
    if hasattr(scene, "witcher_w2scene_active_cutscene_path"):
        scene.witcher_w2scene_active_cutscene_path = ""
    scene.witcher_sections_index = 0
    scene.witcher_w2scene_actor_index = 0
    scene.witcher_w2scene_dialogset_index = 0
    scene.witcher_w2scene_dialogset_slot_index = 0
    scene.witcher_w2scene_camera_index = 0
    scene.witcher_w2scene_element_index = 0
    scene.witcher_w2scene_event_index = 0


def _w2scene_as_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def _w2scene_as_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def _w2scene_iter_values(prop):
    if prop is None:
        return []
    if isinstance(prop, (list, tuple, set)):
        return list(prop)
    for attr in ("value", "More", "elements", "Handles"):
        values = getattr(prop, attr, None)
        if values is not None:
            if isinstance(values, (list, tuple, set)):
                return list(values)
            return [values]
    return []


def _w2scene_ptr_value(ptr):
    if ptr is None:
        return None
    value = getattr(ptr, "Value", ptr)
    return value if isinstance(value, int) else None


def _w2scene_prop_text(value, default=""):
    if value is None:
        return default
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (int, float, str)):
        text = str(value).strip()
        return text or default
    guid = getattr(value, "GUID", None)
    guid_text = str(getattr(guid, "GuidString", "") or "").strip()
    if guid_text:
        return guid_text
    try:
        text = prop_to_string(value)
        if text:
            return text
    except Exception:
        pass
    for attr_name in (
        "String",
        "Value",
        "val",
        "elementID",
        "sectionName",
        "id",
        "name",
        "actorName",
        "slotName",
        "cameraName",
        "eventName",
        "animationName",
    ):
        attr = getattr(value, attr_name, None)
        if attr is None:
            continue
        if hasattr(attr, "val"):
            attr = getattr(attr, "val", "")
        if hasattr(attr, "String"):
            attr = getattr(attr, "String", "")
        text = str(attr or "").strip()
        if text:
            return text
    return default


def _w2scene_localized_line_id(prop):
    line_id = dialog_language.localized_string_id(prop)
    if line_id:
        return line_id
    return _w2scene_prop_text(prop)


def _w2scene_resolve_line_text(line_id, scene_filepath="", context=None):
    language = dialog_language.get_active_text_language(context)
    return dialog_language.resolve_localized_text(line_id, scene_filepath, language=language)


def _w2scene_dialog_display_name(actor, line_text, line_id, fallback="CStorySceneLine"):
    actor = str(actor or "").strip()
    label_text = str(line_text or line_id or "").strip()
    if actor and label_text:
        return f"{actor}: {label_text}"
    return label_text or actor or fallback


def _w2scene_join_values(value):
    if value is None:
        return ""
    values = _w2scene_iter_values(value)
    if not values and isinstance(value, str):
        return value
    if not values:
        text = _w2scene_prop_text(value)
        return text
    parts = []
    for item in values:
        text = _w2scene_prop_text(item)
        if text:
            parts.append(text)
    return "; ".join(parts)


def _w2scene_is_set(value):
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value)
    if isinstance(value, (list, tuple, set)):
        return bool(value)
    values = _w2scene_iter_values(value)
    if values:
        return True
    return True


def _w2scene_fill_field_items(collection, imported_data, schema, section_index=-1):
    if imported_data is None:
        return
    for class_name, fields in schema:
        if class_name != imported_data.__class__.__name__ and not isinstance(imported_data, getattr(w3_types, class_name, object)):
            if class_name not in ("CStorySceneSection", "CStoryScene"):
                continue
        for field_name, _default in fields:
            value = getattr(imported_data, field_name, None)
            item = collection.add()
            item.section_index = int(section_index)
            item.class_name = class_name
            item.field_name = field_name
            item.is_set = _w2scene_is_set(value)
            item.value_text = _format_imported_field_value(value) if item.is_set else "<unset>"


def _w2scene_load_chunk_object(story_scene, ptr_value):
    ptr = _w2scene_ptr_value(ptr_value)
    if not ptr:
        return None
    try:
        chunk = story_scene.chunksRef[ptr - 1]
        cls = w3_types.str_to_class(chunk.Type)
        return cls(chunk)
    except Exception:
        log.debug("Could not parse w2scene chunk pointer %s", ptr, exc_info=True)
        return None


def _w2scene_element_label(element):
    element_type = element.__class__.__name__ if element is not None else "CStorySceneElement"
    element_id = _w2scene_prop_text(getattr(element, "elementID", None))
    if element_type == "CStorySceneLine":
        actor = _w2scene_prop_text(getattr(element, "voicetag", None))
        line_id = _w2scene_prop_text(getattr(element, "dialogLine", None))
        return f"{actor}: {line_id}" if actor or line_id else (element_id or element_type)
    if element_type == "CStoryScenePauseElement":
        return element_id or "Pause"
    if element_type == "CStorySceneScriptLine":
        script = _w2scene_prop_text(getattr(element, "script", None))
        return script[:80] if script else (element_id or element_type)
    return element_id or _w2scene_prop_text(element) or element_type


def _w2scene_event_icon(event_type):
    if "Camera" in event_type:
        return "CAMERA_DATA"
    if "LookAt" in event_type or "Lookat" in event_type or "DialogLine" in event_type:
        return "OUTLINER_OB_SPEAKER"
    if "Mimic" in event_type or "Morph" in event_type:
        return "SHAPEKEY_DATA"
    if "Animation" in event_type or "Pose" in event_type or "Anim" in event_type:
        return "ACTION"
    if "Sound" in event_type:
        return "SOUND"
    if "Effect" in event_type or "Surface" in event_type:
        return "SHADERFX"
    if "Placement" in event_type or "EnterActor" in event_type or "ExitActor" in event_type:
        return "EMPTY_AXIS"
    if "Visibility" in event_type or "Despawn" in event_type:
        return "HIDE_OFF"
    if "Fade" in event_type:
        return "IMAGE_ALPHA"
    return _event_type_icon(event_type)


def _w2scene_event_label(item):
    for attr_name in ("event_name", "animation_name", "camera_name", "effect_name"):
        text = str(getattr(item, attr_name, "") or "").strip()
        if text:
            return text
    return str(getattr(item, "event_type", "") or "Event")


def _w2scene_event_detail_text(event):
    parts = []
    for attr_name in (
        "actor",
        "actorName",
        "target",
        "bodyTarget",
        "eyesTarget",
        "propId",
        "propID",
        "effectName",
        "animationName",
        "customCameraName",
        "level",
        "type",
        "enabled",
        "instant",
        "weight",
        "forceBodyIdleAnimation",
        "transitionAnimation",
    ):
        value = getattr(event, attr_name, None)
        if not _w2scene_is_set(value):
            continue
        text = _format_imported_field_value(value)
        if text and text != "\"\"":
            parts.append(f"{attr_name}={text}")
    return "  ".join(parts[:8])


def _w2scene_event_camera_name(event):
    camera_name = _w2scene_prop_text(getattr(event, "customCameraName", None))
    if camera_name:
        return camera_name
    camera_definition = getattr(event, "cameraDefinition", None)
    if camera_definition is not None:
        try:
            camera_definition = w3_types.StorySceneCameraDefinition(camera_definition)
            return _w2scene_prop_text(getattr(camera_definition, "cameraName", None))
        except Exception:
            return ""
    return ""


def _w2scene_sync_loaded_state(scene, filepath, scene_importer=None):
    _w2scene_clear_loaded_state(scene)
    if not filepath:
        return None

    if scene_importer is None:
        scene_importer = import_scene.import_w3_scene(filepath)
        scene_importer.load_sections()

    story_scene = scene_importer._CStoryScene
    sections = list(getattr(scene_importer, "scene_sections", []) or [])
    scene.witcher_sections_filepath = filepath
    scene.witcher_loaded_w2scene_path = filepath
    scene.witcher_loaded_w2scene_name = os.path.basename(filepath)
    scene.witcher_w2scene_repo_path = _derive_w2scene_repo_path(bpy.context, filepath)

    _w2scene_fill_field_items(scene.witcher_w2scene_root_fields, story_scene, _W2SCENE_ROOT_FIELD_SCHEMA)

    total_elements = 0
    total_events = 0
    cutscene_sections = 0
    for section_index, section in enumerate(sections):
        element_refs = list(getattr(getattr(section, "sceneElements", None), "value", []) or [])
        events = list(getattr(section, "sceneEventElements", []) or [])
        total_elements += len(element_refs)
        total_events += len(events)
        if section.__class__.__name__ == "CStorySceneCutsceneSection":
            cutscene_sections += 1

        duration_overrides = {}
        try:
            variant_id = scene_importer._section_variant_id_for_language(
                section,
                dialog_language.get_active_voice_language(bpy.context),
            )
            duration_overrides = scene_importer._section_variant_duration_overrides(
                section,
                variant_id=variant_id,
            )
        except Exception:
            log.debug("Could not read section duration overrides", exc_info=True)

        section_duration = 0.0
        element_meta_by_ptr = {}
        for element_ref in element_refs:
            element = _w2scene_load_chunk_object(story_scene, element_ref)
            ptr = _w2scene_ptr_value(element_ref)
            element_id = _w2scene_prop_text(getattr(element, "elementID", None))
            duration = duration_overrides.get(
                element_id,
                _w2scene_as_float(
                    getattr(element, "approvedDuration", None)
                    or getattr(element, "duration", None),
                    0.0,
                ),
            )
            element_meta_by_ptr[ptr] = {
                "element": element,
                "element_id": element_id,
                "start": section_duration,
                "duration": duration,
                "label": _w2scene_element_label(element),
            }
            el_item = scene.witcher_w2scene_section_element_items.add()
            el_item.section_index = section_index
            el_item.source_index = ptr or -1
            el_item.element_type = element.__class__.__name__ if element is not None else "Unknown"
            el_item.element_id = element_id
            el_item.display_name = element_meta_by_ptr[ptr]["label"]
            el_item.start_time = section_duration
            el_item.duration = duration
            el_item.actor = _w2scene_prop_text(getattr(element, "voicetag", None))
            el_item.target = _w2scene_prop_text(getattr(element, "speakingTo", None))
            el_item.line_id = _w2scene_localized_line_id(getattr(element, "dialogLine", None))
            if el_item.element_type == "CStorySceneLine" and el_item.line_id:
                el_item.line_text = _w2scene_resolve_line_text(el_item.line_id, filepath, bpy.context)
                el_item.display_name = _w2scene_dialog_display_name(
                    el_item.actor,
                    el_item.line_text,
                    el_item.line_id,
                    fallback=el_item.display_name,
                )
            if getattr(element, "soundEventName", None):
                el_item.detail_text = f"sound={_w2scene_prop_text(getattr(element, 'soundEventName', None))}"
            section_duration += duration

        sec_item = add_scene_section(_w2scene_prop_text(getattr(section, "sectionName", None)) or f"Section {section_index + 1}", "{}", scene)
        sec_item.section_index = section_index
        sec_item.section_type = section.__class__.__name__
        sec_item.section_id = _w2scene_as_int(getattr(section, "sectionId", 0), 0)
        sec_item.element_count = len(element_refs)
        sec_item.event_count = len(events)
        sec_item.duration = section_duration
        sec_item.dialogset_change = _w2scene_prop_text(getattr(section, "dialogsetChangeTo", None))
        sec_item.linked_cutscene = prop_to_string(getattr(section, "cutscene", None))
        sec_item.is_gameplay = bool(getattr(section, "isGameplay", False) or False)
        sec_item.is_important = bool(getattr(section, "isImportant", False) or False)

        _w2scene_fill_field_items(scene.witcher_w2scene_section_fields, section, _W2SCENE_SECTION_FIELD_SCHEMA, section_index=section_index)

        for event_index, event in enumerate(events):
            event_type = event.__class__.__name__
            scene_element_ptr = _w2scene_ptr_value(getattr(event, "sceneElement", None))
            meta = element_meta_by_ptr.get(scene_element_ptr, {})
            element_start = float(meta.get("start", 0.0) or 0.0)
            element_duration = float(meta.get("duration", 0.0) or 0.0)
            start_position = _w2scene_as_float(getattr(event, "startPosition", None), 0.0)
            duration_raw = _w2scene_as_float(getattr(event, "duration", None), 0.0)
            duration = duration_raw
            if 0.0 < duration_raw <= 1.001 and element_duration > 0.0:
                duration = duration_raw * element_duration

            ev_item = scene.witcher_w2scene_section_event_items.add()
            ev_item.section_index = section_index
            ev_item.source_index = event_index
            ev_item.event_type = event_type
            ev_item.event_name = _w2scene_prop_text(getattr(event, "eventName", None))
            ev_item.start_position = start_position
            ev_item.start_time = element_start + (element_duration * start_position)
            ev_item.duration_raw = duration_raw
            ev_item.duration = duration
            ev_item.actor = _w2scene_prop_text(
                getattr(event, "actor", None)
                or getattr(event, "actorName", None)
                or getattr(event, "propId", None)
                or getattr(event, "propID", None)
            )
            ev_item.target = _w2scene_prop_text(
                getattr(event, "target", None)
                or getattr(event, "bodyTarget", None)
                or getattr(event, "eyesTarget", None)
            )
            ev_item.scene_element_id = str(meta.get("element_id", "") or scene_element_ptr or "")
            ev_item.track_name = _w2scene_prop_text(getattr(event, "trackName", None))
            ev_item.animation_name = _w2scene_prop_text(getattr(event, "animationName", None))
            ev_item.camera_name = _w2scene_event_camera_name(event)
            ev_item.effect_name = _w2scene_prop_text(getattr(event, "effectName", None) or getattr(event, "effect", None))
            ev_item.guid = _w2scene_prop_text(getattr(event, "GUID", None))
            ev_item.is_muted = bool(getattr(event, "isMuted", False) or False)
            ev_item.detail_text = _w2scene_event_detail_text(event)

    for actor_index, actor_ref in enumerate(getattr(getattr(story_scene, "sceneTemplates", None), "value", []) or []):
        actor = _w2scene_load_chunk_object(story_scene, actor_ref)
        if actor is None:
            continue
        item = scene.witcher_w2scene_actor_items.add()
        item.item_type = "ACTOR"
        item.source_index = actor_index
        item.actor_id = _w2scene_prop_text(getattr(actor, "id", None))
        item.alias = _w2scene_prop_text(getattr(actor, "alias", None))
        item.actor_tags = _w2scene_join_values(getattr(actor, "actorTags", None))
        item.template_path = _w2scene_prop_text(getattr(actor, "entityTemplate", None))
        item.appearance_filter = _w2scene_join_values(getattr(actor, "appearanceFilter", None))
        item.use_mimic = bool(getattr(actor, "useMimic", False) or False)
        item.force_spawn = bool(getattr(actor, "forceSpawn", False) or False)
        item.dont_search_by_voicetag = bool(getattr(actor, "dontSearchByVoicetag", False) or False)

    for prop_index, prop_ref in enumerate(getattr(getattr(story_scene, "sceneProps", None), "value", []) or []):
        prop = _w2scene_load_chunk_object(story_scene, prop_ref)
        if prop is None:
            continue
        item = scene.witcher_w2scene_actor_items.add()
        item.item_type = "PROP"
        item.source_index = prop_index
        item.actor_id = _w2scene_prop_text(getattr(prop, "id", None))
        item.template_path = _w2scene_prop_text(getattr(prop, "entityTemplate", None))
        item.force_behavior_graph = _w2scene_prop_text(getattr(prop, "forceBehaviorGraph", None))
        item.reset_behavior_graph = bool(getattr(prop, "resetBehaviorGraph", False) or False)
        item.use_mimic = bool(getattr(prop, "useMimics", False) or False)

    for light_index, light_ref in enumerate(getattr(getattr(story_scene, "sceneLights", None), "value", []) or []):
        light = _w2scene_load_chunk_object(story_scene, light_ref)
        if light is None:
            continue
        item = scene.witcher_w2scene_actor_items.add()
        item.item_type = "LIGHT"
        item.source_index = light_index
        item.actor_id = _w2scene_prop_text(getattr(light, "id", None))
        item.light_type = _w2scene_prop_text(getattr(light, "type", None))
        item.shadow_casting_mode = _w2scene_prop_text(getattr(light, "shadowCastingMode", None))
        item.inner_angle = _w2scene_as_float(getattr(light, "innerAngle", None), 0.0)
        item.outer_angle = _w2scene_as_float(getattr(light, "outerAngle", None), 0.0)
        item.softness = _w2scene_as_float(getattr(light, "softness", None), 0.0)
        item.dimmer_type = _w2scene_prop_text(getattr(light, "dimmerType", None))

    actor_template_by_name = {}
    for actor_ref in getattr(getattr(story_scene, "sceneTemplates", None), "value", []) or []:
        actor = _w2scene_load_chunk_object(story_scene, actor_ref)
        if actor is None:
            continue
        template_path = _w2scene_prop_text(getattr(actor, "entityTemplate", None))
        for key in (
            _w2scene_prop_text(getattr(actor, "id", None)),
            _w2scene_prop_text(getattr(actor, "alias", None)),
        ):
            if key:
                actor_template_by_name[key.lower()] = template_path

    for dialogset_index, dialogset_ref in enumerate(getattr(getattr(story_scene, "dialogsetInstances", None), "value", []) or []):
        dialogset = _w2scene_load_chunk_object(story_scene, dialogset_ref)
        if dialogset is None:
            continue
        slot_refs = list(getattr(getattr(dialogset, "slots", None), "value", []) or [])
        item = scene.witcher_w2scene_dialogset_items.add()
        item.source_index = dialogset_index
        item.name = _w2scene_prop_text(getattr(dialogset, "name", None))
        item.placement_tag = _w2scene_join_values(getattr(dialogset, "placementTag", None))
        item.path = _w2scene_prop_text(getattr(dialogset, "path", None))
        item.slot_count = len(slot_refs)
        item.snap_to_terrain = bool(getattr(dialogset, "snapToTerrain", False) or False)
        item.find_safe_placement = bool(getattr(dialogset, "findSafePlacement", False) or False)
        for slot_index, slot_ref in enumerate(slot_refs):
            slot = _w2scene_load_chunk_object(story_scene, slot_ref)
            if slot is None:
                continue
            slot_item = scene.witcher_w2scene_dialogset_slot_items.add()
            slot_item.dialogset_index = dialogset_index
            slot_item.source_index = slot_index
            slot_item.slot_number = _w2scene_as_int(getattr(slot, "slotNumber", 0), 0)
            slot_item.slot_name = _w2scene_prop_text(getattr(slot, "slotName", None))
            slot_item.actor_name = _w2scene_prop_text(getattr(slot, "actorName", None))
            slot_item.actor_status = _w2scene_prop_text(getattr(slot, "actorStatus", None))
            slot_item.actor_pose_name = _w2scene_prop_text(getattr(slot, "actorPoseName", None))
            slot_item.actor_emotional_state = _w2scene_prop_text(getattr(slot, "actorEmotionalState", None))
            slot_item.actor_mimics_state = _w2scene_prop_text(getattr(slot, "actorMimicsEmotionalState", None))
            slot_item.actor_mimics_layer_eyes = _w2scene_prop_text(getattr(slot, "actorMimicsLayer_Eyes", None))
            slot_item.actor_mimics_layer_pose = _w2scene_prop_text(getattr(slot, "actorMimicsLayer_Pose", None))
            slot_item.actor_mimics_layer_animation = _w2scene_prop_text(getattr(slot, "actorMimicsLayer_Animation", None))
            slot_item.actor_mimics_layer_pose_weight = _w2scene_as_float(getattr(slot, "actorMimicsLayer_Pose_Weight", None), 1.0)
            slot_item.force_body_idle_animation = _w2scene_prop_text(getattr(slot, "forceBodyIdleAnimation", None))
            slot_item.force_body_idle_animation_weight = _w2scene_as_float(getattr(slot, "forceBodyIdleAnimationWeight", None), 1.0)
            actor_visibility = getattr(slot, "actorVisibility", True)
            slot_item.actor_visibility = True if actor_visibility is None else bool(actor_visibility)
            slot_item.actor_template_path = actor_template_by_name.get(slot_item.actor_name.lower(), "")
            slot_placement = getattr(slot, "slotPlacement", None)
            engine_transform = getattr(slot_placement, "EngineTransform", None) if slot_placement is not None else None
            if engine_transform is not None:
                slot_item.slot_place_x = float(getattr(engine_transform, "X", 0.0) or 0.0)
                slot_item.slot_place_y = float(getattr(engine_transform, "Y", 0.0) or 0.0)
                slot_item.slot_place_z = float(getattr(engine_transform, "Z", 0.0) or 0.0)
                slot_item.slot_place_yaw = float(getattr(engine_transform, "Yaw", 0.0) or 0.0)
                slot_item.slot_place_pitch = float(getattr(engine_transform, "Pitch", 0.0) or 0.0)
                slot_item.slot_place_roll = float(getattr(engine_transform, "Roll", 0.0) or 0.0)

    for camera_index, camera_def in enumerate(getattr(getattr(story_scene, "cameraDefinitions", None), "More", []) or []):
        try:
            camera = w3_types.StorySceneCameraDefinition(camera_def)
        except Exception:
            log.debug("Could not parse scene camera definition", exc_info=True)
            continue
        item = scene.witcher_w2scene_camera_items.add()
        item.source_index = camera_index
        item.camera_name = _w2scene_prop_text(getattr(camera, "cameraName", None))
        item.fov = _w2scene_as_float(getattr(camera, "cameraFov", None), 0.0)
        item.zoom = _w2scene_as_float(getattr(camera, "cameraZoom", None), 0.0)
        item.source_slot_name = _w2scene_prop_text(getattr(camera, "sourceSlotName", None))
        item.target_slot_name = _w2scene_prop_text(getattr(camera, "targetSlotName", None))
        item.camera_adjust_version = _w2scene_as_int(getattr(camera, "cameraAdjustVersion", 0), 0)
        dof_parts = []
        for attr_name in ("dofFocusDistFar", "dofBlurDistFar", "dofFocusDistNear", "dofBlurDistNear", "dofIntensity"):
            value = getattr(camera, attr_name, None)
            if value is not None:
                dof_parts.append(f"{attr_name}={_w2scene_as_float(value):g}")
        item.dof_summary = "  ".join(dof_parts[:5])

    scene.witcher_w2scene_summary = (
        f"Sections: {len(sections)} ({len(sections) - cutscene_sections} dialog, {cutscene_sections} cutscene), "
        f"elements: {total_elements}, events: {total_events}, "
        f"actors: {sum(1 for item in scene.witcher_w2scene_actor_items if item.item_type == 'ACTOR')}, "
        f"props: {sum(1 for item in scene.witcher_w2scene_actor_items if item.item_type == 'PROP')}, "
        f"lights: {sum(1 for item in scene.witcher_w2scene_actor_items if item.item_type == 'LIGHT')}, "
        f"dialogsets: {len(scene.witcher_w2scene_dialogset_items)}, "
        f"cameras: {len(scene.witcher_w2scene_camera_items)}"
    )
    return scene_importer


def refresh_w2scene_dialog_language(context, refresh_audio=False):
    scene = getattr(context, "scene", None) if context is not None else None
    if scene is None:
        return 0

    scene_filepath = str(
        getattr(scene, "witcher_loaded_w2scene_path", "")
        or getattr(scene, "witcher_sections_filepath", "")
        or ""
    ).strip()
    updated = 0
    for item in getattr(scene, "witcher_w2scene_section_element_items", []) or []:
        if str(getattr(item, "element_type", "") or "") != "CStorySceneLine":
            continue
        line_id = str(getattr(item, "line_id", "") or "").strip()
        if not line_id:
            continue
        text = _w2scene_resolve_line_text(line_id, scene_filepath, context)
        if str(getattr(item, "line_text", "") or "") != text:
            item.line_text = text
            updated += 1
        item.display_name = _w2scene_dialog_display_name(
            getattr(item, "actor", ""),
            text,
            line_id,
            fallback=getattr(item, "display_name", "") or "CStorySceneLine",
        )

        try:
            from .ui_voice import _get_sequence_editor_strips

            strips = _get_sequence_editor_strips(getattr(scene, "sequence_editor", None))
            if strips is not None:
                line_id_int = int(line_id)
                for strip in list(strips):
                    if getattr(strip, "type", None) != 'SOUND':
                        continue
                    strip_line_id = str(
                        strip.get(dialog_language.DIALOG_SUBTITLE_LINE_ID_PROP, "")
                        or strip.get("witcher_cutscene_dialog_line_id", "")
                        or getattr(strip, "name", "")
                        or ""
                    ).split(".", 1)[0].strip()
                    try:
                        if int(strip_line_id) != line_id_int:
                            continue
                    except Exception:
                        continue
                    strip[dialog_language.DIALOG_SUBTITLE_TEXT_PROP] = text
                    strip[dialog_language.DIALOG_SUBTITLE_LINE_ID_PROP] = line_id
                    strip[dialog_language.DIALOG_SUBTITLE_SPEAKER_PROP] = str(getattr(item, "actor", "") or "")
                    strip[dialog_language.DIALOG_SUBTITLE_SOURCE_PROP] = "w2scene"
                    strip[dialog_language.DIALOG_SUBTITLE_SOURCE_PATH_PROP] = scene_filepath
                    strip[dialog_language.DIALOG_SUBTITLE_LANGUAGE_PROP] = dialog_language.get_active_text_language(context)
                    strip["witcher_w2scene_dialog_text"] = text
        except Exception:
            log.debug("Could not update .w2scene audio strip subtitle text for line %s.", line_id, exc_info=True)
    if (
        refresh_audio
        and str(getattr(scene, "witcher_loaded_w2scene_path", "") or "").strip()
        and not str(getattr(scene, "witcher_w2scene_active_cutscene_path", "") or "").strip()
    ):
        try:
            bpy.ops.witcher.load_section()
        except Exception:
            log.warning("Could not refresh .w2scene section audio/lipsync for the new language.", exc_info=True)
    return updated


def _w2scene_is_under_root(path, root):
    path = str(path or "").strip()
    root = str(root or "").strip()
    if not path or not root:
        return False
    try:
        path = os.path.normcase(os.path.abspath(os.path.normpath(path)))
        root = os.path.normcase(os.path.abspath(os.path.normpath(root)))
        return path == root or path.startswith(root.rstrip("\\/") + os.sep)
    except Exception:
        path = os.path.normcase(os.path.normpath(path))
        root = os.path.normcase(os.path.normpath(root)).rstrip("\\/")
        return path == root or path.startswith(root + "\\") or path.startswith(root + "/")


def _w2scene_normalize_repo_path(path):
    return str(path or "").replace("/", "\\").lstrip("\\")


def _w2scene_add_unique_root(roots, root):
    root = str(root or "").strip()
    if not root:
        return
    root_norm = os.path.normpath(root)
    try:
        root_norm = bpy.path.abspath(root_norm)
    except Exception:
        pass
    if not os.path.isdir(root_norm):
        return
    root_key = os.path.normcase(root_norm)
    if all(os.path.normcase(existing) != root_key for existing in roots):
        roots.append(root_norm)


def _w2scene_repo_root_from_filepath(filepath, repo_path=""):
    normalized = os.path.normpath(str(filepath or ""))
    if not normalized:
        return ""
    repo_path = _w2scene_normalize_repo_path(repo_path)
    if repo_path:
        repo_as_fs = repo_path.replace("\\", os.sep)
        path_key = os.path.normcase(normalized)
        repo_key = os.path.normcase(repo_as_fs)
        if path_key.endswith(repo_key):
            root = normalized[:len(normalized) - len(repo_as_fs)].rstrip("\\/")
            if root:
                return root

    lowered = normalized.lower()
    for marker in ("\\r4data\\", "\\workspace\\", "\\content\\content0\\"):
        marker_index = lowered.find(marker)
        if marker_index >= 0:
            return normalized[:marker_index + len(marker) - 1]
    return ""


def _w2scene_pref_repo_roots(context):
    roots = []
    try:
        prefs = get_all_addon_prefs(context)
    except Exception:
        prefs = None
    if prefs is None:
        return roots

    try:
        projects = list(getattr(prefs, "redkit_projects", []) or [])
    except Exception:
        projects = []
    for project in projects:
        project_path = str(getattr(project, "path", "") or "").strip()
        if project_path:
            _w2scene_add_unique_root(roots, os.path.join(bpy.path.abspath(project_path), "workspace"))

    for attr_name in ("redkit_depot_path", "redkit_uncooked_path"):
        try:
            _w2scene_add_unique_root(roots, bpy.path.abspath(getattr(prefs, attr_name, "") or ""))
        except Exception:
            pass

    try:
        _w2scene_add_unique_root(roots, get_uncook_path(context))
    except Exception:
        pass
    return roots


def _w2scene_repo_roots_for_loaded_scene(context, filepath="", repo_path=""):
    roots = []
    scene = getattr(context, "scene", None)
    filepath = str(filepath or getattr(scene, "witcher_sections_filepath", "") or getattr(scene, "witcher_loaded_w2scene_path", "") or "")
    repo_path = str(repo_path or getattr(scene, "witcher_w2scene_repo_path", "") or "")
    _w2scene_add_unique_root(roots, _w2scene_repo_root_from_filepath(filepath, repo_path))
    for root in _w2scene_pref_repo_roots(context):
        _w2scene_add_unique_root(roots, root)
    return roots


def _w2scene_root_kind(context, root):
    root = os.path.normpath(str(root or ""))
    try:
        uncook_root = os.path.normpath(get_uncook_path(context))
    except Exception:
        uncook_root = ""
    if uncook_root and _same_filesystem_path(root, uncook_root):
        return "cooked r4data"
    lower = root.lower()
    if lower.endswith("\\workspace") or "\\workspace\\" in lower:
        return "REDkit project workspace"
    if lower.endswith("\\r4data") or "\\r4data\\" in lower:
        return "REDkit depot/source"
    if "redkit" in lower:
        return "REDkit source"
    return "repo root"


def _w2scene_cutscene_dependency_roots(context, roots):
    project_roots = []
    cooked_roots = []
    source_roots = []
    other_roots = []
    for root in roots or []:
        kind = _w2scene_root_kind(context, root)
        if kind == "REDkit project workspace":
            _w2scene_add_unique_root(project_roots, root)
        elif kind == "cooked r4data":
            _w2scene_add_unique_root(cooked_roots, root)
        elif kind.startswith("REDkit"):
            _w2scene_add_unique_root(source_roots, root)
        else:
            _w2scene_add_unique_root(other_roots, root)

    ordered = []
    for root_group in (project_roots, cooked_roots, other_roots, source_roots):
        for root in root_group:
            _w2scene_add_unique_root(ordered, root)
    return ordered


def _w2scene_log_cutscene_resolution(context, cutscene_repo_path, cutscene_path, dependency_roots):
    try:
        uncook_root = os.path.normpath(get_uncook_path(context))
    except Exception:
        uncook_root = ""
    cutscene_kind = _w2scene_root_kind(context, cutscene_path)
    if uncook_root and not _w2scene_is_under_root(cutscene_path, uncook_root):
        log.warning(
            "W2Scene linked cutscene is being loaded from %s, not cooked r4data: %s -> %s",
            cutscene_kind,
            cutscene_repo_path,
            cutscene_path,
        )
    if dependency_roots:
        root_summary = "; ".join(
            f"{idx + 1}. {_w2scene_root_kind(context, root)}={root}"
            for idx, root in enumerate(dependency_roots)
        )
        log.warning(
            "W2Scene linked cutscene dependency search order: %s",
            root_summary,
        )


def _w2scene_repo_path_from_root(filepath, root):
    filepath = os.path.normpath(str(filepath or ""))
    root = os.path.normpath(str(root or ""))
    if not filepath or not root:
        return ""
    try:
        rel = os.path.relpath(filepath, root)
    except ValueError:
        return ""
    if rel.startswith("..") or os.path.isabs(rel):
        return ""
    return rel.replace("/", "\\")


def _w2scene_resolve_repo_file(context, repo_path, roots):
    repo_path = _w2scene_normalize_repo_path(repo_path)
    if not repo_path:
        return ""
    if os.path.isabs(repo_path):
        return repo_path
    repo_as_fs = repo_path.replace("\\", os.sep)
    for root in roots or []:
        candidate = os.path.normpath(os.path.join(root, repo_as_fs))
        if os.path.isfile(candidate):
            return candidate
    try:
        return repo_file(repo_path)
    except Exception:
        return os.path.join(get_uncook_path(context), repo_as_fs)


def _resolve_w2scene_cutscene_file(context, section):
    cutscene_repo_path = prop_to_string(getattr(section, "cutscene", None))
    cutscene_repo_path = _w2scene_normalize_repo_path(cutscene_repo_path)
    if not cutscene_repo_path:
        return "", "", []
    if os.path.isabs(cutscene_repo_path):
        return cutscene_repo_path, cutscene_repo_path, []
    scene = getattr(context, "scene", None)
    scene_filepath = str(getattr(scene, "witcher_sections_filepath", "") or getattr(scene, "witcher_loaded_w2scene_path", "") or "")
    scene_repo_path = str(getattr(scene, "witcher_w2scene_repo_path", "") or "")
    roots = _w2scene_repo_roots_for_loaded_scene(context, scene_filepath, scene_repo_path)
    cutscene_path = _w2scene_resolve_repo_file(context, cutscene_repo_path, roots)
    return cutscene_repo_path, str(cutscene_path or "").strip(), roots


def _w2scene_collect_object_tree(root_obj):
    object_names = set()

    def add_object_tree_names(start_obj):
        pending = [start_obj] if start_obj is not None else []
        while pending:
            obj = pending.pop(0)
            try:
                obj_name = str(getattr(obj, "name", "") or "")
            except ReferenceError:
                continue
            if not obj_name or obj_name in object_names:
                continue
            object_names.add(obj_name)
            try:
                pending.extend(list(getattr(obj, "children", []) or []))
            except ReferenceError:
                pass

    add_object_tree_names(root_obj)

    mimic_name = ""
    try:
        mimic_name = str(root_obj.get("mimicFace", "") or "").strip() if root_obj is not None else ""
    except ReferenceError:
        mimic_name = ""
    mimic_obj = bpy.data.objects.get(mimic_name) if mimic_name else None
    add_object_tree_names(mimic_obj)

    try:
        cutscene_guid = str(root_obj.get(import_cutscene.CUTSCENE_GUID_PROP, "") or "").strip() if root_obj is not None else ""
    except ReferenceError:
        cutscene_guid = ""
    if cutscene_guid:
        for obj in list(bpy.data.objects):
            try:
                if obj.get(import_cutscene.CUTSCENE_GUID_PROP) == cutscene_guid:
                    object_names.add(str(obj.name))
            except ReferenceError:
                pass
            except Exception:
                pass
    return object_names


def _w2scene_force_remove_objects(object_names):
    def parent_depth(obj_name):
        obj = bpy.data.objects.get(obj_name)
        if obj is None:
            return -1
        depth = 0
        try:
            parent = getattr(obj, "parent", None)
        except ReferenceError:
            return -1
        while parent is not None:
            depth += 1
            try:
                parent = getattr(parent, "parent", None)
            except ReferenceError:
                break
        return depth

    removed = 0
    for obj_name in sorted(set(object_names or []), key=parent_depth, reverse=True):
        obj = bpy.data.objects.get(obj_name)
        if obj is None:
            continue
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
            removed += 1
        except ReferenceError:
            pass
        except Exception:
            log.debug("Could not force-remove section cutscene object %s", obj_name, exc_info=True)
    return removed


def _w2scene_unload_active_cutscene_section(context):
    scene = getattr(context, "scene", None)
    if scene is None:
        return {"actors": 0, "audio": 0, "nla": 0}
    active_path = str(getattr(scene, "witcher_w2scene_active_cutscene_path", "") or "").strip()
    if not active_path:
        return {"actors": 0, "audio": 0, "nla": 0}

    removed_audio = 0
    removed_nla = 0
    removed_actors = 0
    try:
        removed_audio += int(import_cutscene.remove_cutscene_burned_audio_strips(scene, source_path=active_path) or 0)
    except Exception:
        log.debug("Could not remove section cutscene burned audio for %s", active_path, exc_info=True)
    try:
        removed_audio += int(import_scene.clear_w2scene_section_audio(scene) or 0)
    except Exception:
        log.debug("Could not remove section cutscene dialog audio", exc_info=True)

    loaded_path = str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or "").strip()
    if not _same_filesystem_path(active_path, loaded_path):
        scene.witcher_w2scene_active_cutscene_path = ""
        return {"actors": 0, "audio": removed_audio, "nla": 0}

    for actor_entry in list(getattr(scene, "witcher_cutscene_actor_items", []) or []):
        actor_obj = _get_loaded_cutscene_actor_object(actor_entry)
        if actor_obj is None:
            continue
        imported_by_cutscene = bool(getattr(actor_entry, "imported_by_cutscene", False))
        if not imported_by_cutscene:
            try:
                imported_by_cutscene = bool(actor_obj.get(import_cutscene.CUTSCENE_ACTOR_IMPORTED_PROP, False))
            except Exception:
                imported_by_cutscene = False
        force_remove_objects = _w2scene_collect_object_tree(actor_obj) if imported_by_cutscene else set()
        try:
            removed_nla += int(import_scene.clear_w2scene_actor_section_nla(context, actor_obj) or 0)
        except Exception:
            log.debug("Could not clear section voice tracks from %s", getattr(actor_obj, "name", ""), exc_info=True)
        try:
            removed_actors += int(import_cutscene.unload_cutscene_actor(actor_obj) or 0)
        except Exception:
            log.exception("Failed to unload section cutscene actor %s", getattr(actor_obj, "name", "<unknown>"))
        if force_remove_objects:
            removed_actors += _w2scene_force_remove_objects(force_remove_objects)

    _clear_loaded_cutscene_state(scene)
    scene.witcher_w2scene_active_cutscene_path = ""
    return {"actors": removed_actors, "audio": removed_audio, "nla": removed_nla}


def _load_w2scene_cutscene_section(context, scene_importer, section):
    scene = context.scene
    cutscene_repo_path, cutscene_path, repo_roots = _resolve_w2scene_cutscene_file(context, section)
    if not cutscene_path:
        raise RuntimeError("Cutscene section has no linked .w2cutscene file.")
    if not os.path.isfile(cutscene_path):
        raise RuntimeError(f"Linked cutscene was not found: {cutscene_repo_path} -> {cutscene_path}")

    _w2scene_unload_active_cutscene_section(context)
    removed = import_scene.clear_w2scene_runtime_state(
        context,
        story_scene=getattr(scene_importer, "_CStoryScene", None),
        reset_actors=True,
    )
    if any(int(value or 0) for value in removed.values()):
        log.info(
            "Cleared previous .w2scene section state before loading cutscene section: %s",
            removed,
        )

    previous_roots, previous_read_only = get_repo_override_state()
    try:
        override_roots = list(repo_roots or [])
        for root in previous_roots:
            _w2scene_add_unique_root(override_roots, root)
        override_roots = _w2scene_cutscene_dependency_roots(context, override_roots)
        _w2scene_log_cutscene_resolution(context, cutscene_repo_path, cutscene_path, override_roots)
        if override_roots:
            set_repo_override_roots(override_roots, read_only=True)

        cutscene_data = import_cutscene.import_w3_cutscene(
            cutscene_path,
            selected_actor_indices=None,
            selected_animation_indices=None,
            auto_apply_selected_animations=True,
            import_burned_audio=True,
        )
        if cutscene_data is None:
            raise RuntimeError(f"Failed to load linked cutscene: {cutscene_path}")

        _sync_loaded_cutscene_state(scene, cutscene_path, cutscene_data=cutscene_data)
        scene.witcher_w2scene_active_cutscene_path = cutscene_path

        dialog_stats = {"loaded": 0, "skipped": 0, "total": 0}
        try:
            dialog_stats = _load_cutscene_dialogs_into_scene(context)
        except Exception as exc:
            log.warning("Cutscene section loaded, but dialog auto-load failed: %s", exc)
    finally:
        set_repo_override_roots(previous_roots, read_only=previous_read_only)
    return {
        "repo_path": cutscene_repo_path,
        "path": cutscene_path,
        "cutscene_data": cutscene_data,
        "dialog_stats": dialog_stats,
        "repo_roots": repo_roots,
    }


def _derive_w2scene_repo_path(context, filepath):
    normalized = os.path.normpath(str(filepath or ""))
    if not normalized:
        return ""

    for root in _w2scene_repo_roots_for_loaded_scene(context, filepath):
        repo_path = _w2scene_repo_path_from_root(normalized, root)
        if repo_path:
            return repo_path

    lowered = normalized.lower()
    for marker in ("\\r4data\\", "\\workspace\\", "\\content\\content0\\"):
        marker_index = lowered.find(marker)
        if marker_index >= 0:
            return normalized[marker_index + len(marker):].replace("/", "\\")
    return ""


def _update_w2scene_preview(operator, context):
    filepath = str(getattr(operator, "filepath", "") or "").strip()
    if not filepath:
        operator.w2scene_preview_status = "Select a .w2scene file"
        operator.w2scene_repo_path = ""
        operator.w2scene_section_summary = ""
        operator.w2scene_actor_summary = ""
        operator.w2scene_cutscene_summary = ""
        operator.w2scene_first_cutscene = ""
        return True

    try:
        mtime = os.path.getmtime(filepath)
    except OSError:
        mtime = 0.0

    same_file = (
        filepath == getattr(operator, "w2scene_preview_path", "")
        and mtime == getattr(operator, "w2scene_preview_mtime", 0.0)
    )
    if same_file:
        return False

    operator.w2scene_preview_path = filepath
    operator.w2scene_preview_mtime = mtime
    operator.w2scene_repo_path = _derive_w2scene_repo_path(context, filepath)

    if not filepath.lower().endswith(".w2scene"):
        operator.w2scene_preview_status = "Select a .w2scene file"
        operator.w2scene_section_summary = ""
        operator.w2scene_actor_summary = ""
        operator.w2scene_cutscene_summary = ""
        operator.w2scene_first_cutscene = ""
        return True

    try:
        scene_importer = import_scene.import_w3_scene(filepath)
        scene_importer.load_sections()
        story_scene = scene_importer._CStoryScene
    except Exception as exc:
        operator.w2scene_preview_status = f"Preview failed: {exc}"
        operator.w2scene_section_summary = ""
        operator.w2scene_actor_summary = ""
        operator.w2scene_cutscene_summary = ""
        operator.w2scene_first_cutscene = ""
        return True

    sections = list(getattr(scene_importer, "scene_sections", []) or [])
    cutscene_sections = [section for section in sections if section.__class__.__name__ == "CStorySceneCutsceneSection"]
    element_count = sum(len(getattr(getattr(section, "sceneElements", None), "value", []) or []) for section in sections)
    event_count = sum(len(getattr(section, "sceneEventElements", []) or []) for section in sections)
    actor_count = len(getattr(getattr(story_scene, "sceneTemplates", None), "value", []) or [])
    dialogset_count = len(getattr(getattr(story_scene, "dialogsetInstances", None), "value", []) or [])

    linked_cutscenes = []
    for section in cutscene_sections:
        cutscene_path = prop_to_string(getattr(section, "cutscene", None))
        if cutscene_path:
            linked_cutscenes.append(cutscene_path)

    operator.w2scene_preview_status = os.path.basename(filepath)
    operator.w2scene_section_summary = (
        f"Sections: {len(sections)} ({len(sections) - len(cutscene_sections)} dialog, "
        f"{len(cutscene_sections)} cutscene), elements: {element_count}, events: {event_count}"
    )
    operator.w2scene_actor_summary = f"Entities: {actor_count}, dialogsets: {dialogset_count}"
    operator.w2scene_cutscene_summary = f"Linked cutscenes: {len(linked_cutscenes)}"
    operator.w2scene_first_cutscene = linked_cutscenes[0] if linked_cutscenes else ""
    return True


class ButtonOperatorImportW2scene(bpy.types.Operator, ImportHelper):
    """Import W2 Cutscee"""
    bl_idname = "witcher.import_w2_scene"
    bl_label = "W2 Scene"
    filename_ext = ".w2scene"
    filter_glob: StringProperty(default='*.w2scene', options={'HIDDEN'})
    w2scene_preview_status: StringProperty(default="Select a .w2scene file")
    w2scene_preview_path: StringProperty(default="")
    w2scene_preview_mtime: FloatProperty(default=0.0)
    w2scene_repo_path: StringProperty(default="")
    w2scene_section_summary: StringProperty(default="")
    w2scene_actor_summary: StringProperty(default="")
    w2scene_cutscene_summary: StringProperty(default="")
    w2scene_first_cutscene: StringProperty(default="")

    def draw(self, context):
        layout = self.layout
        preview_box = layout.box()
        preview_box.label(text="Scene Preview")
        preview_box.label(text=self.w2scene_preview_status, icon='SCENE_DATA')
        if self.w2scene_repo_path:
            preview_box.label(text=f"Repo Path: {self.w2scene_repo_path}", icon='FILE')
        if self.w2scene_section_summary:
            preview_box.label(text=self.w2scene_section_summary)
        if self.w2scene_actor_summary:
            preview_box.label(text=self.w2scene_actor_summary)
        if self.w2scene_cutscene_summary:
            preview_box.label(text=self.w2scene_cutscene_summary)
        if self.w2scene_first_cutscene:
            preview_box.label(text=f"First cutscene: {self.w2scene_first_cutscene}", icon='SEQUENCE')

    def check(self, context):
        return _update_w2scene_preview(self, context)

    def execute(self, context):
        if os.path.isdir(self.filepath):
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}
        if not self.filepath.lower().endswith(".w2scene"):
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}
        try:
            _w2scene_unload_active_cutscene_section(context)
            sceneImporter = import_scene.import_w3_scene(self.filepath)
            sceneImporter.load_sections()
            _w2scene_sync_loaded_state(context.scene, self.filepath, scene_importer=sceneImporter)
        except Exception as exc:
            log.exception("Failed to load scene sections from %s", self.filepath)
            self.report({'ERROR'}, f"Failed to load scene: {exc}")
            return {'CANCELLED'}
        #sceneImporter.execute()
        bpy.context.view_layer.update()
        return {'FINISHED'}
    def invoke(self, context, event):
        UNCOOK_PATH = os.path.join(get_uncook_path(context),"animations\\")
        if os.path.exists(UNCOOK_PATH):
            self.filepath = UNCOOK_PATH if self.filepath == '' else self.filepath
        _update_w2scene_preview(self, context)
        return ImportHelper.invoke(self, context, event)


def _w2scene_active_section_index(scene):
    sections = list(getattr(scene, "witcher_sections", []) or [])
    idx = int(getattr(scene, "witcher_sections_index", 0) or 0)
    if 0 <= idx < len(sections):
        return int(getattr(sections[idx], "section_index", idx))
    return -1


def _w2scene_selected_item(collection, active_index, predicate):
    items = list(collection or [])
    if 0 <= active_index < len(items) and predicate(items[active_index]):
        return items[active_index]
    for item in items:
        if predicate(item):
            return item
    return None


def _w2scene_element_icon(element_type):
    if element_type == "CStorySceneLine":
        return "OUTLINER_OB_SPEAKER"
    if element_type == "CStoryScenePauseElement":
        return "TIME"
    if "Choice" in element_type:
        return "HELP"
    if "Script" in element_type:
        return "CONSOLE"
    if "Cutscene" in element_type or "Video" in element_type:
        return "SEQUENCE"
    return "TEXT"


class WITCH_UL_W2SceneActorList(UIList):
    bl_idname = "WITCH_UL_W2SceneActorList"
    layout_type = "DEFAULT"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index, flt_flag):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            item_type = str(getattr(item, "item_type", "ACTOR") or "ACTOR")
            icon_name = 'ARMATURE_DATA'
            if item_type == "PROP":
                icon_name = 'OUTLINER_OB_EMPTY'
            elif item_type == "LIGHT":
                icon_name = 'LIGHT_DATA'
            split = row.split(factor=0.54)
            split.label(text=item.actor_id or f"{item_type.title()} {index + 1}", icon=icon_name)
            meta = split.row(align=True)
            meta.alignment = 'RIGHT'
            meta.label(text=item_type.title())
            if item_type == "ACTOR" and item.appearance_filter:
                meta.label(text=item.appearance_filter, icon='MATERIAL_DATA')
            elif item_type == "PROP" and item.template_path:
                meta.label(text=os.path.basename(item.template_path.replace("\\", "/")))
            elif item_type == "LIGHT" and (item.light_type or item.shadow_casting_mode):
                meta.label(text=item.light_type or item.shadow_casting_mode)
            if item.use_mimic:
                meta.label(text="mimic", icon='SHAPEKEY_DATA')
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text="")


class WITCH_UL_W2SceneDialogsetList(UIList):
    bl_idname = "WITCH_UL_W2SceneDialogsetList"
    layout_type = "DEFAULT"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index, flt_flag):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            row.label(text=item.name or f"Dialogset {index + 1}", icon='OUTLINER_COLLECTION')
            if item.placement_tag:
                row.label(text=item.placement_tag, icon='EMPTY_AXIS')
            row.label(text=f"{item.slot_count} slots")
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text="")


class WITCH_UL_W2SceneDialogsetSlotList(UIList):
    bl_idname = "WITCH_UL_W2SceneDialogsetSlotList"
    layout_type = "DEFAULT"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index, flt_flag):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            row.label(text=item.slot_name or f"Slot {item.slot_number}", icon='EMPTY_AXIS')
            row.label(text=item.actor_name or "-", icon='ARMATURE_DATA')
            if item.actor_pose_name:
                row.label(text=item.actor_pose_name, icon='POSE_HLT')
            if item.force_body_idle_animation:
                row.label(text=item.force_body_idle_animation, icon='ACTION')
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text="")

    def filter_items(self, context, data, propname):
        items = getattr(data, propname, [])
        dialogset_idx = int(getattr(context.scene, "witcher_w2scene_dialogset_index", 0) or 0)
        flags = [
            self.bitflag_filter_item
            if int(getattr(item, "dialogset_index", -1)) == dialogset_idx
            else 0
            for item in items
        ]
        return flags, []


class WITCH_UL_W2SceneCameraList(UIList):
    bl_idname = "WITCH_UL_W2SceneCameraList"
    layout_type = "DEFAULT"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index, flt_flag):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            row.label(text=item.camera_name or f"Camera {index + 1}", icon='CAMERA_DATA')
            if item.fov:
                row.label(text=f"FOV {item.fov:g}")
            if item.source_slot_name or item.target_slot_name:
                row.label(text=f"{item.source_slot_name or '-'} -> {item.target_slot_name or '-'}")
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text="")


class WITCH_UL_W2SceneElementList(UIList):
    bl_idname = "WITCH_UL_W2SceneElementList"
    layout_type = "DEFAULT"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index, flt_flag):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            row.label(text=item.display_name or item.element_id or item.element_type, icon=_w2scene_element_icon(item.element_type))
            cls_badge = row.row(align=True)
            cls_badge.enabled = False
            cls_badge.scale_x = 0.75
            cls_badge.label(text=item.element_type.replace("CStoryScene", ""))
            row.label(text=f"{item.start_time:.2f}s")
            if item.duration > 0.0:
                row.label(text=f"+{item.duration:.2f}s")
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text="")

    def filter_items(self, context, data, propname):
        items = getattr(data, propname, [])
        active_section = _w2scene_active_section_index(context.scene)
        flags = [
            self.bitflag_filter_item
            if int(getattr(item, "section_index", -1)) == active_section
            else 0
            for item in items
        ]
        return flags, []


class WITCH_UL_W2SceneEventList(UIList):
    bl_idname = "WITCH_UL_W2SceneEventList"
    layout_type = "DEFAULT"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index, flt_flag):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            row.label(text=_w2scene_event_label(item), icon=_w2scene_event_icon(item.event_type))
            cls_badge = row.row(align=True)
            cls_badge.enabled = False
            cls_badge.scale_x = 0.75
            cls_badge.label(text=item.event_type.replace("CStorySceneEvent", "").replace("CStoryScene", ""))
            if item.actor:
                row.label(text=item.actor, icon='ARMATURE_DATA')
            if item.target:
                row.label(text=f"-> {item.target}")
            row.label(text=f"{item.start_time:.2f}s")
            if item.duration > 0.0:
                row.label(text=f"+{item.duration:.2f}s")
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text="")

    def filter_items(self, context, data, propname):
        items = getattr(data, propname, [])
        active_section = _w2scene_active_section_index(context.scene)
        flags = [
            self.bitflag_filter_item
            if int(getattr(item, "section_index", -1)) == active_section
            else 0
            for item in items
        ]
        return flags, []


def _draw_w2scene_readonly_props(layout, item, fields):
    col = layout.column(align=True)
    col.use_property_split = True
    col.enabled = False
    for prop_name, label in fields:
        if hasattr(item, prop_name):
            col.prop(item, prop_name, text=label)


def _draw_w2scene_scene_tab(layout, scene):
    path = str(getattr(scene, "witcher_loaded_w2scene_path", "") or "").strip()
    repo_path = str(getattr(scene, "witcher_w2scene_repo_path", "") or "").strip()
    if path:
        layout.label(text=path, icon='FILE')
    if repo_path:
        layout.label(text=f"Repo Path: {repo_path}", icon='FILEBROWSER')
    summary = str(getattr(scene, "witcher_w2scene_summary", "") or "").strip()
    if summary:
        layout.label(text=summary, icon='INFO')

    field_box = layout.box()
    header = field_box.row(align=True)
    header.label(text="Scene Fields", icon='PROPERTIES')
    header.prop(scene, "witcher_w2scene_show_unset_fields", text="Show Unset", toggle=True)
    _draw_imported_class_sections(
        field_box,
        list(getattr(scene, "witcher_w2scene_root_fields", [])),
        _W2SCENE_ROOT_FIELD_SCHEMA,
        bool(getattr(scene, "witcher_w2scene_show_unset_fields", False)),
        "No scene fields loaded.",
    )


def _draw_w2scene_sections_tab(layout, scene):
    sections = list(getattr(scene, "witcher_sections", []) or [])
    if not sections:
        layout.label(text="No .w2scene loaded.", icon='INFO')
        return

    col = layout.column(align=True)
    col.template_list("WITCHER_SECTIONS_UL_List", "", scene, "witcher_sections", scene, "witcher_sections_index", rows=min(len(sections), 10))

    active_idx = int(getattr(scene, "witcher_sections_index", 0) or 0)
    if not (0 <= active_idx < len(sections)):
        return
    section = sections[active_idx]
    section_index = int(getattr(section, "section_index", active_idx))

    detail = layout.box()
    hdr = detail.row(align=True)
    hdr.label(text=section.name or f"Section {active_idx + 1}", icon='SEQUENCE')
    hdr.label(text=section.section_type or "CStorySceneSection")
    if section.linked_cutscene:
        detail.label(text=f"Linked cutscene: {section.linked_cutscene}", icon='ACTION')
    detail.label(
        text=(
            f"Duration {section.duration:.3f}s  "
            f"Elements {section.element_count}  Events {section.event_count}"
        ),
        icon='TIME',
    )
    flag_row = detail.row(align=True)
    flag_row.enabled = False
    flag_row.label(text=f"sectionId={section.section_id}")
    flag_row.label(text=f"gameplay={section.is_gameplay}")
    flag_row.label(text=f"important={section.is_important}")
    if section.dialogset_change:
        flag_row.label(text=f"dialogset={section.dialogset_change}")

    detail.separator(factor=0.5)
    elem_box = detail.box()
    elem_box.label(text="Elements", icon='TEXT')
    elem_box.template_list(
        "WITCH_UL_W2SceneElementList", "",
        scene, "witcher_w2scene_section_element_items",
        scene, "witcher_w2scene_element_index",
        rows=min(max(1, section.element_count), 7),
    )
    elem = _w2scene_selected_item(
        getattr(scene, "witcher_w2scene_section_element_items", []),
        int(getattr(scene, "witcher_w2scene_element_index", 0) or 0),
        lambda item: int(getattr(item, "section_index", -1)) == section_index,
    )
    if elem is not None:
        _draw_w2scene_readonly_props(elem_box, elem, [
            ("element_type", "type"),
            ("element_id", "elementID"),
            ("display_name", "name"),
            ("start_time", "start"),
            ("duration", "duration"),
            ("actor", "actor"),
            ("target", "target"),
            ("line_text", "LocalizedString.text"),
            ("line_id", "dialogLine"),
            ("detail_text", "details"),
        ])

    event_box = detail.box()
    event_box.label(text="Events", icon='KEYFRAME')
    event_box.template_list(
        "WITCH_UL_W2SceneEventList", "",
        scene, "witcher_w2scene_section_event_items",
        scene, "witcher_w2scene_event_index",
        rows=min(max(1, section.event_count), 9),
    )
    event = _w2scene_selected_item(
        getattr(scene, "witcher_w2scene_section_event_items", []),
        int(getattr(scene, "witcher_w2scene_event_index", 0) or 0),
        lambda item: int(getattr(item, "section_index", -1)) == section_index,
    )
    if event is not None:
        ev_detail = event_box.box()
        ev_header = ev_detail.row(align=True)
        ev_header.label(text=event.event_type or "Event", icon=_w2scene_event_icon(event.event_type))
        if str(getattr(event, "event_type", "") or "") in {"CStorySceneEventCustomCamera", "CStorySceneEventCustomCameraInstance"}:
            op = ev_header.operator(WITCH_OT_W2ScenePreviewCameraEvent.bl_idname, text="Preview", icon='VIEW_CAMERA')
            op.section_index = section_index
            op.source_index = int(getattr(event, "source_index", -1))
        _draw_w2scene_readonly_props(ev_detail, event, [
            ("event_name", "eventName"),
            ("scene_element_id", "sceneElement"),
            ("start_time", "start"),
            ("start_position", "startPosition"),
            ("duration", "duration"),
            ("duration_raw", "rawDuration"),
            ("actor", "actor"),
            ("target", "target"),
            ("track_name", "track"),
            ("animation_name", "animation"),
            ("camera_name", "camera"),
            ("effect_name", "effect"),
            ("guid", "GUID"),
            ("is_muted", "muted"),
            ("detail_text", "details"),
        ])

    fields = [
        item for item in getattr(scene, "witcher_w2scene_section_fields", [])
        if int(getattr(item, "section_index", -1)) == section_index
    ]
    fields_box = detail.box()
    header = fields_box.row(align=True)
    header.label(text="Section Fields", icon='PROPERTIES')
    header.prop(scene, "witcher_w2scene_show_unset_fields", text="Show Unset", toggle=True)
    _draw_imported_class_sections(
        fields_box,
        fields,
        _W2SCENE_SECTION_FIELD_SCHEMA,
        bool(getattr(scene, "witcher_w2scene_show_unset_fields", False)),
        "No set section fields.",
    )


def _draw_w2scene_actors_tab(layout, scene):
    items = list(getattr(scene, "witcher_w2scene_actor_items", []) or [])
    if not items:
        layout.label(text="No scene actors, props or lights.", icon='INFO')
        return
    layout.template_list(
        "WITCH_UL_W2SceneActorList", "",
        scene, "witcher_w2scene_actor_items",
        scene, "witcher_w2scene_actor_index",
        rows=min(len(items), 10),
    )
    idx = int(getattr(scene, "witcher_w2scene_actor_index", 0) or 0)
    if 0 <= idx < len(items):
        item = items[idx]
        item_type = str(getattr(item, "item_type", "ACTOR") or "ACTOR")
        detail = layout.box()
        icon_name = 'ARMATURE_DATA'
        if item_type == "PROP":
            icon_name = 'OUTLINER_OB_EMPTY'
        elif item_type == "LIGHT":
            icon_name = 'LIGHT_DATA'
        header_row = detail.row(align=True)
        header_row.label(text=item.actor_id or f"{item_type.title()} {idx + 1}", icon=icon_name)
        action_row = header_row.row(align=True)
        action_row.alignment = 'RIGHT'
        action_row.enabled = item_type in {"ACTOR", "PROP"} and bool(getattr(item, "template_path", ""))
        op = action_row.operator(
            WITCH_OT_W2SceneImportActorItem.bl_idname,
            text="Import",
            icon='IMPORT',
        )
        op.item_index = idx
        if item_type == "ACTOR":
            _draw_w2scene_readonly_props(detail, item, [
                ("item_type", "kind"),
                ("actor_id", "id"),
                ("alias", "alias"),
                ("actor_tags", "actorTags"),
                ("template_path", "entityTemplate"),
                ("appearance_filter", "appearanceFilter"),
                ("use_mimic", "useMimic"),
                ("force_spawn", "forceSpawn"),
                ("dont_search_by_voicetag", "dontSearchByVoicetag"),
            ])
        elif item_type == "PROP":
            _draw_w2scene_readonly_props(detail, item, [
                ("item_type", "kind"),
                ("actor_id", "id"),
                ("template_path", "entityTemplate"),
                ("force_behavior_graph", "forceBehaviorGraph"),
                ("reset_behavior_graph", "resetBehaviorGraph"),
                ("use_mimic", "useMimics"),
            ])
        elif item_type == "LIGHT":
            _draw_w2scene_readonly_props(detail, item, [
                ("item_type", "kind"),
                ("actor_id", "id"),
                ("light_type", "type"),
                ("shadow_casting_mode", "shadowCastingMode"),
                ("inner_angle", "innerAngle"),
                ("outer_angle", "outerAngle"),
                ("softness", "softness"),
                ("dimmer_type", "dimmerType"),
            ])


def _draw_w2scene_dialogsets_tab(layout, scene, context):
    dialogsets = list(getattr(scene, "witcher_w2scene_dialogset_items", []) or [])
    if not dialogsets:
        layout.label(text="No dialogsets.", icon='INFO')
        return
    layout.template_list(
        "WITCH_UL_W2SceneDialogsetList", "",
        scene, "witcher_w2scene_dialogset_items",
        scene, "witcher_w2scene_dialogset_index",
        rows=min(len(dialogsets), 6),
    )
    idx = int(getattr(scene, "witcher_w2scene_dialogset_index", 0) or 0)
    if not (0 <= idx < len(dialogsets)):
        return
    dialogset = dialogsets[idx]
    detail = layout.box()
    detail.label(text=dialogset.name or f"Dialogset {idx + 1}", icon='OUTLINER_COLLECTION')
    _draw_w2scene_readonly_props(detail, dialogset, [
        ("name", "name"),
        ("placement_tag", "placementTag"),
        ("path", "path"),
        ("slot_count", "slots"),
        ("snap_to_terrain", "snapToTerrain"),
        ("find_safe_placement", "findSafePlacement"),
    ])
    detail.separator(factor=0.5)
    detail.label(text="Slots", icon='EMPTY_AXIS')
    detail.template_list(
        "WITCH_UL_W2SceneDialogsetSlotList", "",
        scene, "witcher_w2scene_dialogset_slot_items",
        scene, "witcher_w2scene_dialogset_slot_index",
        rows=min(max(1, dialogset.slot_count), 8),
    )
    slot = _w2scene_selected_item(
        getattr(scene, "witcher_w2scene_dialogset_slot_items", []),
        int(getattr(scene, "witcher_w2scene_dialogset_slot_index", 0) or 0),
        lambda item: int(getattr(item, "dialogset_index", -1)) == idx,
    )
    if slot is not None:
        slot_box = detail.box()
        slot_box.label(text=slot.slot_name or f"Slot {slot.slot_number}", icon='EMPTY_AXIS')

        wm = context.window_manager
        slot_key = f"{idx}:{slot.source_index}"
        if getattr(wm, "witcher_dialogset_active_slot_key", "") != slot_key:
            global _DIALOGSET_SYNCING_SLOT_PROPS
            _DIALOGSET_SYNCING_SLOT_PROPS = True

            try:
                wm.witcher_dialogset_active_slot_key = slot_key

                def _set_enum(prop_name, value, fallback):
                    cleaned = str(value or "").strip()
                    for candidate in (cleaned, fallback):
                        if not candidate:
                            continue
                        try:
                            setattr(wm, prop_name, candidate)
                            return
                        except (TypeError, ValueError):
                            continue

                _set_enum("witcher_dialogset_status", slot.actor_status, "High")
                _set_enum("witcher_dialogset_emotional_state", slot.actor_emotional_state, "Determined")
                _set_enum("witcher_dialogset_pose_name", slot.actor_pose_name, "Standing")
                _set_enum("witcher_dialogset_mimics_state", slot.actor_mimics_state, "None")
                _set_enum("witcher_dialogset_mimics_layer_eyes", slot.actor_mimics_layer_eyes, "None")
                _set_enum("witcher_dialogset_mimics_layer_pose", slot.actor_mimics_layer_pose, "None")
                _set_enum("witcher_dialogset_mimics_layer_animation", slot.actor_mimics_layer_animation, "None")
                wm.witcher_dialogset_mimics_layer_pose_weight = float(slot.actor_mimics_layer_pose_weight)
                wm.witcher_dialogset_force_body_idle_animation = slot.force_body_idle_animation or ""
                wm.witcher_dialogset_force_body_idle_animation_weight = float(slot.force_body_idle_animation_weight)
            finally:
                _DIALOGSET_SYNCING_SLOT_PROPS = False

        _draw_w2scene_readonly_props(slot_box, slot, [
            ("slot_number", "slotNumber"),
            ("slot_name", "slotName"),
            ("actor_name", "actorName"),
            ("actor_visibility", "actorVisibility"),
            ("actor_template_path", "entityTemplate"),
            ("slot_place_x", "slotPlacement.X"),
            ("slot_place_y", "slotPlacement.Y"),
            ("slot_place_z", "slotPlacement.Z"),
            ("slot_place_yaw", "slotPlacement.Yaw"),
            ("slot_place_pitch", "slotPlacement.Pitch"),
            ("slot_place_roll", "slotPlacement.Roll"),
        ])
        slot_box.separator(factor=0.3)
        slot_box.label(text="Manual Slot Preview", icon='ARMATURE_DATA')
        slot_box.prop(wm, "witcher_dialogset_status", text="actorStatus")
        slot_box.prop(wm, "witcher_dialogset_emotional_state", text="actorEmotionalState")
        slot_box.prop(wm, "witcher_dialogset_pose_name", text="actorPoseName")
        slot_box.prop(wm, "witcher_dialogset_mimics_state", text="actorMimicsEmotionalState")
        slot_box.prop(wm, "witcher_dialogset_mimics_layer_eyes", text="actorMimicsLayer_Eyes")
        slot_box.prop(wm, "witcher_dialogset_mimics_layer_pose", text="actorMimicsLayer_Pose")
        slot_box.prop(wm, "witcher_dialogset_mimics_layer_animation", text="actorMimicsLayer_Animation")
        slot_box.prop(wm, "witcher_dialogset_mimics_layer_pose_weight", text="actorMimicsLayer_Pose_Weight", slider=True)
        slot_box.prop(wm, "witcher_dialogset_force_body_idle_animation", text="forceBodyIdleAnimation")
        slot_box.prop(wm, "witcher_dialogset_force_body_idle_animation_weight", text="forceBodyIdleAnimationWeight", slider=True)

        mimics_entry = _parse_mimics_csv().get(str(wm.witcher_dialogset_mimics_state or "").lower(), {})
        for label, layer_value, layer_column in (
            ("Eyes", wm.witcher_dialogset_mimics_layer_eyes, "eyes"),
            ("Pose", wm.witcher_dialogset_mimics_layer_pose, "pose"),
            ("Anim", wm.witcher_dialogset_mimics_layer_animation, "animation"),
        ):
            resolved = _resolve_mimic_layer_anim(layer_value, layer_column) or mimics_entry.get(layer_column, "")
            if resolved:
                slot_box.row().label(text=f"  -> {label}: {resolved}", icon='ANIM_DATA')

        slot_box.separator(factor=0.3)
        load_row = slot_box.row(align=True)
        load_op = load_row.operator(WITCH_OT_LoadDialogsetSlotActor.bl_idname, text="Load Actor + Pose", icon='IMPORT')
        load_op.dialogset_index = idx
        load_op.slot_source_index = slot.source_index
        load_row.enabled = bool(slot.actor_template_path)

        apply_row = slot_box.row(align=True)
        body_op = apply_row.operator(WITCH_OT_ApplyDialogsetSlotPose.bl_idname, text="Apply Pose", icon='PLAY')
        body_op.dialogset_index = idx
        body_op.slot_source_index = slot.source_index
        mimics_op = apply_row.operator(WITCH_OT_ApplyDialogsetSlotMimics.bl_idname, text="Apply Mimics", icon='HIDE_OFF')
        mimics_op.dialogset_index = idx
        mimics_op.slot_source_index = slot.source_index


def _draw_w2scene_cameras_tab(layout, scene, context):
    camera_arm = _w2scene_find_camera_armature(context)
    rig_box = layout.box()
    rig_box.label(text="Scene Camera Rig", icon='CAMERA_DATA')
    if camera_arm is None:
        rig_box.label(text="Load a section to import the scene camera rig.", icon='INFO')
    else:
        hdr = rig_box.row(align=True)
        hdr.label(text=getattr(camera_arm, "name", "Scene Camera"), icon='ARMATURE_DATA')
        preview_row = rig_box.row(align=True)
        preview_row.operator("witcher.camera_setup_preview", text="Setup Preview", icon='CAMERA_DATA')
        preview_row.operator("witcher.camera_set_scene_camera", text="Set Scene Camera", icon='VIEW_CAMERA')
        key_row = rig_box.row(align=True)
        key_row.operator("witcher.camera_key_rig_from_scene_camera", text="Key Rig + DOF", icon='KEY_HLT')
        _draw_camera_track_controls(rig_box, camera_arm)

    layout.separator(factor=0.5)
    cameras = list(getattr(scene, "witcher_w2scene_camera_items", []) or [])
    if not cameras:
        layout.label(text="No camera definitions.", icon='INFO')
        return
    layout.template_list(
        "WITCH_UL_W2SceneCameraList", "",
        scene, "witcher_w2scene_camera_items",
        scene, "witcher_w2scene_camera_index",
        rows=min(len(cameras), 8),
    )
    idx = int(getattr(scene, "witcher_w2scene_camera_index", 0) or 0)
    if 0 <= idx < len(cameras):
        detail = layout.box()
        detail.label(text=cameras[idx].camera_name or f"Camera {idx + 1}", icon='CAMERA_DATA')
        _draw_w2scene_readonly_props(detail, cameras[idx], [
            ("camera_name", "cameraName"),
            ("fov", "cameraFov"),
            ("zoom", "cameraZoom"),
            ("source_slot_name", "sourceSlotName"),
            ("target_slot_name", "targetSlotName"),
            ("camera_adjust_version", "cameraAdjustVersion"),
            ("dof_summary", "DOF"),
        ])


def _draw_w2scene_panel(layout, scene):
    layout.label(text="Scene (.w2scene)", icon='WORLD')
    action_row = layout.row(align=True)
    action_row.operator(ButtonOperatorImportW2scene.bl_idname, text="Import Scene (.w2scene)", icon='IMPORT')
    if hasattr(scene, dialog_language.DIALOG_TEXT_LANGUAGE_PROP):
        action_row.prop(scene, dialog_language.DIALOG_TEXT_LANGUAGE_PROP, text="Text")
    if hasattr(scene, dialog_language.DIALOG_VOICE_LANGUAGE_PROP):
        action_row.prop(scene, dialog_language.DIALOG_VOICE_LANGUAGE_PROP, text="Voice")
    if hasattr(scene, "witcher_cutscene_show_dialog_subtitles"):
        action_row.prop(scene, "witcher_cutscene_show_dialog_subtitles", text="Subtitles")
    if getattr(scene, "witcher_loaded_w2scene_path", ""):
        action_row.operator(Witcher_OT_load_section.bl_idname, text="Load Section", icon='SEQUENCE')
        action_row.prop(scene, "witcher_w2scene_write_profile_log", text="Profile Log")
        action_row.prop(scene, "witcher_w2scene_create_debug_markers", text="Debug Markers")

    active_cutscene_path = str(getattr(scene, "witcher_w2scene_active_cutscene_path", "") or "").strip()
    if active_cutscene_path:
        layout.label(text=f"Active cutscene section: {os.path.basename(active_cutscene_path)}", icon='ACTION')

    if not getattr(scene, "witcher_loaded_w2scene_path", ""):
        if getattr(scene, "witcher_sections_filepath", ""):
            layout.label(text=str(getattr(scene, "witcher_sections_filepath", "")), icon='FILE')
        layout.label(text="Import a .w2scene to inspect sections, elements, events and dialogsets.", icon='INFO')
        return

    tab_row = layout.row(align=True)
    tab_row.scale_y = 1.2
    tab_row.prop_enum(scene, "witcher_w2scene_tab", 'SCENE')
    tab_row.prop_enum(scene, "witcher_w2scene_tab", 'SECTIONS')
    tab_row.prop_enum(scene, "witcher_w2scene_tab", 'ACTORS')
    tab_row.prop_enum(scene, "witcher_w2scene_tab", 'DIALOGSETS')
    tab_row.prop_enum(scene, "witcher_w2scene_tab", 'CAMERAS')
    layout.separator(factor=0.5)

    tab = str(getattr(scene, "witcher_w2scene_tab", "SECTIONS") or "SECTIONS")
    if tab == 'SCENE':
        _draw_w2scene_scene_tab(layout, scene)
    elif tab == 'SECTIONS':
        _draw_w2scene_sections_tab(layout, scene)
    elif tab == 'ACTORS':
        _draw_w2scene_actors_tab(layout, scene)
    elif tab == 'DIALOGSETS':
        _draw_w2scene_dialogsets_tab(layout, scene, bpy.context)
    elif tab == 'CAMERAS':
        _draw_w2scene_cameras_tab(layout, scene, bpy.context)


class WITCHER_PT_scene_panel(WITCH_PT_Base, Panel):
    bl_idname = "WITCHER_PT_scene_panel"
    bl_label = "Scene"
    bl_description = ""
    bl_options = {'DEFAULT_CLOSED'}

    def draw_header(self, context):
        self.layout.label(text="", icon='SCENE_DATA')

    def draw(self, context):
        scene = context.scene
        if scene is None:
            return
        _draw_w2scene_panel(self.layout, scene)

class WITCHER_SECTIONS_UL_List(UIList):
    bl_idname = "WITCHER_SECTIONS_UL_List"
    layout_type = "DEFAULT"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index, flt_flag):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=False)
            type_name = str(getattr(item, "section_type", "") or "")
            icon_name = 'ACTION' if type_name == "CStorySceneCutsceneSection" else 'SEQUENCE'
            row.label(text=item.name or f"Section {index + 1}", icon=icon_name)
            dur = getattr(item, "duration", 0.0) or 0.0
            right = row.row()
            right.alignment = 'RIGHT'
            right.label(text=f"{dur:.2f}s")
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text="")

class Witcher_OT_load_section(bpy.types.Operator):
    bl_idname = "witcher.load_section"
    bl_label = "Load Scene Section"
    bl_description = "Load the selected .w2scene section"

    def execute(self, context):
        # Get the scene object
        scene = context.scene

        # Print the index of the selected scene section
        log.debug("Selected Index: %s", scene.witcher_sections_index)

        try:
            sceneImporter = import_scene.import_w3_scene(context.scene.witcher_sections_filepath)
            sceneImporter.load_sections()
            this_section = sceneImporter.scene_sections[scene.witcher_sections_index]
        except Exception as exc:
            log.exception("Failed to load selected .w2scene section.")
            self.report({'ERROR'}, f"Failed to load section: {exc}")
            return {'CANCELLED'}

        section_name = str(getattr(this_section, "sectionName", "") or "")
        log.debug("Section: %s", section_name)
        profile_job = _new_w2scene_import_profile_job_state()
        if bool(getattr(scene, "witcher_w2scene_write_profile_log", True)):
            _start_w2scene_import_profile_log(
                profile_job,
                scene_path=context.scene.witcher_sections_filepath,
                section_name=section_name,
            )
        if this_section.__class__.__name__ == "CStorySceneCutsceneSection":
            try:
                result = _load_w2scene_cutscene_section(context, sceneImporter, this_section)
            except Exception as exc:
                log.exception("Failed to load cutscene section %s", section_name)
                _stop_w2scene_import_profile_log(profile_job, f".w2scene cutscene section import failed: {section_name}")
                self.report({'ERROR'}, f"Failed to load cutscene section: {exc}")
                return {'CANCELLED'}

            cutscene_name = os.path.basename(result.get("path", ""))
            dialog_stats = dict(result.get("dialog_stats", {}) or {})
            dialog_total = int(dialog_stats.get("total", 0) or 0)
            dialog_loaded = int(dialog_stats.get("loaded", 0) or 0)
            dialog_text = f", dialog {dialog_loaded}/{dialog_total}" if dialog_total else ""
            _stop_w2scene_import_profile_log(profile_job, f".w2scene cutscene section import completed: {section_name}")
            self.report({'INFO'}, f"Loaded cutscene section '{section_name}' from {cutscene_name}{dialog_text}.")
            return {'FINISHED'}

        _w2scene_unload_active_cutscene_section(context)
        try:
            sceneImporter.load_section(this_section)
            sceneImporter.execute()
        except Exception as exc:
            log.exception("Failed to load section %s", section_name)
            _stop_w2scene_import_profile_log(profile_job, f".w2scene section import failed: {section_name}")
            self.report({'ERROR'}, f"Failed to load section: {exc}")
            return {'CANCELLED'}

        _stop_w2scene_import_profile_log(profile_job, f".w2scene section import completed: {section_name}")
        return {'FINISHED'}



class WITCH_OT_W2ScenePreviewCameraEvent(bpy.types.Operator):
    bl_idname = "witcher.w2scene_preview_camera_event"
    bl_label = "Preview Scene Camera Event"
    bl_description = "Apply the selected .w2scene camera event to the scene camera rig for inspection"
    bl_options = {'REGISTER', 'UNDO'}

    section_index: IntProperty(default=-1)
    source_index: IntProperty(default=-1)

    def execute(self, context):
        scene = context.scene
        filepath = str(
            getattr(scene, "witcher_sections_filepath", "")
            or getattr(scene, "witcher_loaded_w2scene_path", "")
            or ""
        )
        if not filepath:
            self.report({'ERROR'}, "No .w2scene file is loaded.")
            return {'CANCELLED'}

        section_index = self.section_index
        if section_index < 0:
            section_index = int(getattr(scene, "witcher_sections_index", 0) or 0)

        try:
            scene_importer = import_scene.import_w3_scene(filepath)
            scene_importer.load_sections()
            if not (0 <= section_index < len(scene_importer.scene_sections)):
                raise IndexError(f"Section index {section_index} is out of range")
            scene_importer.load_section(scene_importer.scene_sections[section_index])
            events = list(getattr(scene_importer, "_section_scene_event_elements", []) or [])
            if not (0 <= self.source_index < len(events)):
                raise IndexError(f"Event index {self.source_index} is out of range")
            event = events[self.source_index]
            if event.__class__.__name__ not in {"CStorySceneEventCustomCamera", "CStorySceneEventCustomCameraInstance"}:
                self.report({'WARNING'}, f"{event.__class__.__name__} has no camera pose to preview.")
                return {'CANCELLED'}
            camera_name, event_frame = scene_importer.preview_camera_event(context, event)
        except Exception as exc:
            log.exception("Failed to preview .w2scene camera event")
            self.report({'ERROR'}, f"Failed to preview camera event: {exc}")
            return {'CANCELLED'}

        self.report({'INFO'}, f"Previewed {camera_name} at frame {event_frame:.2f}.")
        return {'FINISHED'}


class WITCH_OT_W2SceneImportActorItem(bpy.types.Operator):
    bl_idname = "witcher.w2scene_import_actor_item"
    bl_label = "Import Selected"
    bl_description = "Import the selected actor or prop's entity template into the scene"
    bl_options = {'REGISTER', 'UNDO'}

    item_index: IntProperty(default=-1)

    def execute(self, context):
        from ..importers import import_entity
        scene = context.scene
        items = list(getattr(scene, "witcher_w2scene_actor_items", []) or [])
        idx = self.item_index if self.item_index >= 0 else int(getattr(scene, "witcher_w2scene_actor_index", 0) or 0)
        if not (0 <= idx < len(items)):
            self.report({'ERROR'}, "No actor or prop selected.")
            return {'CANCELLED'}
        item = items[idx]
        item_type = str(getattr(item, "item_type", "") or "ACTOR")
        template_path = str(getattr(item, "template_path", "") or "").strip()
        if not template_path:
            self.report({'ERROR'}, f"Selected {item_type.lower()} has no entityTemplate path.")
            return {'CANCELLED'}
        if item_type == "LIGHT":
            self.report({'WARNING'}, "Lights are placed by section load, not imported as entities.")
            return {'CANCELLED'}

        scene_filepath = str(getattr(scene, "witcher_loaded_w2scene_path", "") or "")
        resolved_path = import_scene._resolve_w2scene_template_path(template_path, scene_filepath)
        if not resolved_path or not os.path.isfile(resolved_path):
            self.report({'ERROR'}, f"Could not resolve template path: {template_path}")
            return {'CANCELLED'}

        before_ids = {id(obj) for obj in bpy.data.objects}
        try:
            if item_type == "ACTOR":
                appearance_filter = str(getattr(item, "appearance_filter", "") or "")
                preferred_appearance = appearance_filter.split(",")[0].strip() if appearance_filter else ""
                imported = import_entity.import_ent_template(
                    resolved_path,
                    load_face_poses=True,
                    import_apperance=1,
                    selected_appearance_name=preferred_appearance,
                )
            else:
                imported = import_entity.import_ent_template(resolved_path)
        except Exception as exc:
            log.exception("Failed to import %s '%s' from %s", item_type.lower(), getattr(item, "actor_id", "?"), resolved_path)
            self.report({'ERROR'}, f"Import failed: {exc}")
            return {'CANCELLED'}

        new_objects = [obj for obj in bpy.data.objects if id(obj) not in before_ids]
        if imported is None and not new_objects:
            self.report({'WARNING'}, f"No objects produced for {os.path.basename(resolved_path)}.")
            return {'CANCELLED'}

        # Stamp prop metadata so subsequent section loads recognize the existing object.
        if item_type == "PROP":
            prop_id = str(getattr(item, "actor_id", "") or "").strip()
            target = imported if imported is not None else (new_objects[0] if new_objects else None)
            if target is not None and prop_id:
                target["witcher_w2scene_prop_id"] = prop_id
                target["witcher_w2scene_prop_template"] = template_path
                target["witcher_scene_item_type"] = "PROP"
                try:
                    for child in target.children_recursive:
                        child["witcher_w2scene_prop_id"] = prop_id
                        child["witcher_w2scene_prop_template"] = template_path
                        child["witcher_scene_item_type"] = "PROP"
                except Exception:
                    pass

        self.report({'INFO'}, f"Imported {item_type.lower()} {getattr(item, 'actor_id', '')} from {os.path.basename(resolved_path)}.")
        return {'FINISHED'}


# Manual dialogset slot preview/edit helpers. Section loading stays in SceneImporter;
# these operators let a selected slot drive actor import and pose/mimic preview.
def _find_dialogset_slot_item(scene, dialogset_index, slot_source_index):
    for item in getattr(scene, "witcher_w2scene_dialogset_slot_items", []):
        if item.dialogset_index == dialogset_index and item.source_index == slot_source_index:
            return item
    return None


def _find_dialogset_placement_obj(scene, dialogset_index):
    from ..importers.import_cutscene import check_if_actor_already_in_scene

    for ds_item in getattr(scene, "witcher_w2scene_dialogset_items", []):
        if ds_item.source_index != dialogset_index:
            continue
        placement_tag = str(ds_item.placement_tag or "").strip()
        if not placement_tag:
            return None
        for actor_item in getattr(scene, "witcher_w2scene_actor_items", []):
            if actor_item.item_type != "ACTOR":
                continue
            tags = [tag.strip() for tag in str(actor_item.actor_tags or "").split(",") if tag.strip()]
            if placement_tag in tags:
                return check_if_actor_already_in_scene(actor_item.template_path) or None
        return None
    return None


def _apply_slot_placement(actor_obj, slot, placement_obj):
    from ..importers.import_helpers import set_blender_object_transform

    class _SlotTransform:
        X = slot.slot_place_x
        Y = slot.slot_place_y
        Z = slot.slot_place_z
        Yaw = slot.slot_place_yaw
        Pitch = slot.slot_place_pitch
        Roll = slot.slot_place_roll
        Scale_x = 1.0
        Scale_y = 1.0
        Scale_z = 1.0

    set_blender_object_transform(actor_obj, _SlotTransform(), from_this_object=placement_obj)


def _load_slot_anim(context, actor_obj, anim_name, track_name, face_target_mode="auto"):
    from ..ui.ui_anims_list import SetupActor, GetAnimationInfoByName, load_anim_into_scene

    SetupActor(actor_obj, context=context)
    resolved_anim_name, fdir = GetAnimationInfoByName(
        anim_name,
        actor_obj,
        compatible_only=True,
    )
    if not resolved_anim_name or not fdir:
        log.warning("Dialogset slot animation '%s' was not found in the animation catalogs", anim_name)
        return False
    try:
        load_anim_into_scene(
            context,
            resolved_anim_name,
            fdir,
            actor_obj,
            NLA_track=track_name,
            at_frame=0,
            face_target_mode=face_target_mode,
        )
        return True
    except Exception:
        log.warning(
            "Failed to load dialogset slot animation '%s' onto '%s'",
            anim_name,
            getattr(actor_obj, "name", "?"),
            exc_info=True,
        )
        return False


def _load_slot_face_layer(context, actor_obj, layer_value, layer_column, track_name):
    from ..ui.ui_anims_list import SetupActor, GetAnimationInfoByName, load_anim_into_scene

    candidates = _resolve_mimic_layer_anim_candidates(layer_value, layer_column)
    if not candidates:
        return False

    SetupActor(actor_obj, context=context)
    last_err = None
    for candidate in candidates:
        resolved_anim_name, fdir = GetAnimationInfoByName(candidate, actor_obj, prefer_mimic=True, quiet=True)
        if not resolved_anim_name or not fdir:
            continue
        try:
            load_anim_into_scene(
                context,
                resolved_anim_name,
                fdir,
                actor_obj,
                NLA_track=track_name,
                at_frame=0,
                face_target_mode="owner",
            )
            log.info(
                "Loaded dialogset mimic layer '%s' using '%s' onto '%s'",
                layer_value,
                candidate,
                getattr(actor_obj, "name", "?"),
            )
            return True
        except Exception as exc:
            last_err = exc
            log.debug("Dialogset mimic candidate '%s' failed: %s", candidate, exc)

    log.warning(
        "No catalog entry found for dialogset mimic layer '%s' (column '%s'); tried: %s",
        layer_value,
        layer_column,
        ", ".join(candidates),
    )
    if last_err is not None:
        log.warning("Last dialogset mimic load error: %s", last_err)
    return False


def _apply_dialogset_pose_weight_to_loaded_actor(context, actor_obj, weight):
    if actor_obj is None:
        return 0
    try:
        from . import ui_mimics
        return ui_mimics.set_dialogset_mimic_pose_weight(
            context,
            weight,
            actor_obj=actor_obj,
            fallback_to_scene=False,
        )
    except Exception:
        log.debug("Failed to update dialogset mimic pose weight.", exc_info=True)
    return 0


def _on_dialogset_slot_mimics_pose_weight_changed(self, context):
    if _DIALOGSET_SYNCING_SLOT_PROPS:
        return
    if context is None:
        return
    scene = getattr(context, "scene", None)
    wm = getattr(context, "window_manager", None)
    if scene is None or wm is None:
        return

    actor_obj = None
    slot_key = str(getattr(wm, "witcher_dialogset_active_slot_key", "") or "")
    if ":" in slot_key:
        try:
            dialogset_index_text, slot_source_index_text = slot_key.split(":", 1)
            slot = _find_dialogset_slot_item(scene, int(dialogset_index_text), int(slot_source_index_text))
        except Exception:
            slot = None
        if slot is not None and getattr(slot, "actor_template_path", ""):
            try:
                from ..importers.import_cutscene import check_if_actor_already_in_scene
                actor_obj = check_if_actor_already_in_scene(slot.actor_template_path)
            except Exception:
                actor_obj = None

    _apply_dialogset_pose_weight_to_loaded_actor(
        context,
        actor_obj,
        getattr(wm, "witcher_dialogset_mimics_layer_pose_weight", 1.0),
    )


class WITCH_OT_LoadDialogsetSlotActor(bpy.types.Operator):
    bl_idname = "witcher.load_dialogset_slot_actor"
    bl_label = "Load Slot Actor"
    bl_description = "Import the selected dialogset slot actor and apply the slot placement and idle pose"
    bl_options = {'REGISTER', 'UNDO'}

    dialogset_index: IntProperty(default=-1)
    slot_source_index: IntProperty(default=-1)

    def execute(self, context):
        from ..importers import import_entity
        from ..importers.import_cutscene import check_if_actor_already_in_scene

        scene = context.scene
        slot = _find_dialogset_slot_item(scene, self.dialogset_index, self.slot_source_index)
        if slot is None:
            self.report({'ERROR'}, "Slot not found")
            return {'CANCELLED'}
        if not slot.actor_template_path:
            self.report({'ERROR'}, f"No entity template for slot '{slot.slot_name}'; reload the scene file")
            return {'CANCELLED'}

        scene_filepath = str(getattr(scene, "witcher_loaded_w2scene_path", "") or "")
        resolved_path = import_scene._resolve_w2scene_template_path(slot.actor_template_path, scene_filepath)
        if not resolved_path or not os.path.isfile(resolved_path):
            self.report({'ERROR'}, f"Could not find entity file: {slot.actor_template_path}")
            return {'CANCELLED'}

        actor_obj = check_if_actor_already_in_scene(slot.actor_template_path)
        if not actor_obj:
            try:
                actor_obj = import_entity.import_ent_template(resolved_path, load_face_poses=True, import_apperance=1)
            except Exception as exc:
                log.exception("Failed to import actor for dialogset slot '%s'", slot.slot_name)
                self.report({'ERROR'}, f"Import failed: {exc}")
                return {'CANCELLED'}
        if actor_obj is None:
            self.report({'ERROR'}, f"Failed to import actor '{slot.actor_name}'")
            return {'CANCELLED'}

        placement_obj = _find_dialogset_placement_obj(scene, self.dialogset_index)
        _apply_slot_placement(actor_obj, slot, placement_obj)

        wm = context.window_manager
        anim_name = str(wm.witcher_dialogset_force_body_idle_animation or "").strip()
        if not anim_name or anim_name.upper() == "NONE":
            anim_name = _lookup_dialogset_body_anim(
                wm.witcher_dialogset_status,
                wm.witcher_dialogset_emotional_state,
                wm.witcher_dialogset_pose_name,
            ) or ""
        if anim_name and not _load_slot_anim(context, actor_obj, anim_name, _DIALOGSET_IDLE_TRACK):
            self.report({'WARNING'}, f"Could not load idle animation: {anim_name}")
        elif not anim_name:
            self.report({'INFO'}, "No idle animation found; check Status, Emotional State and Pose")

        return {'FINISHED'}


class WITCH_OT_ApplyDialogsetSlotPose(bpy.types.Operator):
    bl_idname = "witcher.apply_dialogset_slot_pose"
    bl_label = "Apply Pose"
    bl_description = "Apply the selected dialogset slot pose to the actor already in the scene"
    bl_options = {'REGISTER', 'UNDO'}

    dialogset_index: IntProperty(default=-1)
    slot_source_index: IntProperty(default=-1)

    def execute(self, context):
        from ..importers.import_cutscene import check_if_actor_already_in_scene

        scene = context.scene
        slot = _find_dialogset_slot_item(scene, self.dialogset_index, self.slot_source_index)
        if slot is None:
            self.report({'ERROR'}, "Slot not found")
            return {'CANCELLED'}

        actor_obj = check_if_actor_already_in_scene(slot.actor_template_path) if slot.actor_template_path else None
        if not actor_obj:
            self.report({'ERROR'}, "Actor not in scene; use Load Actor + Pose first")
            return {'CANCELLED'}

        wm = context.window_manager
        anim_name = str(wm.witcher_dialogset_force_body_idle_animation or "").strip()
        if not anim_name or anim_name.upper() == "NONE":
            anim_name = _lookup_dialogset_body_anim(
                wm.witcher_dialogset_status,
                wm.witcher_dialogset_emotional_state,
                wm.witcher_dialogset_pose_name,
            ) or ""

        if not anim_name:
            self.report({'WARNING'}, "No idle animation found for selected pose")
            return {'CANCELLED'}

        if not _load_slot_anim(context, actor_obj, anim_name, _DIALOGSET_IDLE_TRACK):
            self.report({'WARNING'}, f"Could not load idle animation: {anim_name}")
            return {'CANCELLED'}
        return {'FINISHED'}


class WITCH_OT_ApplyDialogsetSlotMimics(bpy.types.Operator):
    bl_idname = "witcher.apply_dialogset_slot_mimics"
    bl_label = "Apply Mimics"
    bl_description = "Apply the selected dialogset slot mimics state to the actor's face rig"
    bl_options = {'REGISTER', 'UNDO'}

    dialogset_index: IntProperty(default=-1)
    slot_source_index: IntProperty(default=-1)

    def execute(self, context):
        from ..importers.import_cutscene import check_if_actor_already_in_scene

        scene = context.scene
        slot = _find_dialogset_slot_item(scene, self.dialogset_index, self.slot_source_index)
        if slot is None:
            self.report({'ERROR'}, "Slot not found")
            return {'CANCELLED'}

        actor_obj = check_if_actor_already_in_scene(slot.actor_template_path) if slot.actor_template_path else None
        if not actor_obj:
            self.report({'ERROR'}, "Actor not in scene; use Load Actor + Pose first")
            return {'CANCELLED'}

        wm = context.window_manager
        state_name = str(wm.witcher_dialogset_mimics_state or "").strip()
        layer_inputs = (
            (wm.witcher_dialogset_mimics_layer_eyes, state_name, "eyes", "SceneDialogsetMimicsEyes"),
            (wm.witcher_dialogset_mimics_layer_pose, state_name, "pose", "SceneDialogsetMimicsPose"),
            (wm.witcher_dialogset_mimics_layer_animation, state_name, "animation", "SceneDialogsetMimicsAnim"),
        )

        loaded_any = False
        for layer_value, fallback_state, layer_column, track_name in layer_inputs:
            value = str(layer_value or "").strip()
            if not value or value.upper() == "NONE":
                value = fallback_state
            if not value or value.upper() == "NONE":
                continue
            if _load_slot_face_layer(context, actor_obj, value, layer_column, track_name):
                loaded_any = True
                if layer_column == "pose":
                    _apply_dialogset_pose_weight_to_loaded_actor(
                        context,
                        actor_obj,
                        getattr(wm, "witcher_dialogset_mimics_layer_pose_weight", 1.0),
                    )

        if not loaded_any:
            self.report({'WARNING'}, "No mimic animations found on this actor")
        return {'FINISHED'}



classes = [
    WitcherSection,
    W2SceneFieldItem,
    W2SceneActorItem,
    W2SceneDialogsetItem,
    W2SceneDialogsetSlotItem,
    W2SceneCameraItem,
    W2SceneSectionElementItem,
    W2SceneSectionEventItem,
    WITCH_UL_W2SceneActorList,
    WITCH_UL_W2SceneDialogsetList,
    WITCH_UL_W2SceneDialogsetSlotList,
    WITCH_UL_W2SceneCameraList,
    WITCH_UL_W2SceneElementList,
    WITCH_UL_W2SceneEventList,
    ButtonOperatorImportW2scene,
    WITCHER_PT_scene_panel,
    WITCHER_SECTIONS_UL_List,
    Witcher_OT_load_section,
    WITCH_OT_W2ScenePreviewCameraEvent,
    WITCH_OT_W2SceneImportActorItem,
    WITCH_OT_LoadDialogsetSlotActor,
    WITCH_OT_ApplyDialogsetSlotPose,
    WITCH_OT_ApplyDialogsetSlotMimics,
]




def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.witcher_sections = bpy.props.CollectionProperty(type=WitcherSection)
    bpy.types.Scene.witcher_sections_index = bpy.props.IntProperty(default=0)
    bpy.types.Scene.witcher_sections_filepath = bpy.props.StringProperty(default="")
    bpy.types.Scene.witcher_loaded_w2scene_name = bpy.props.StringProperty(default="")
    bpy.types.Scene.witcher_loaded_w2scene_path = bpy.props.StringProperty(default="")
    bpy.types.Scene.witcher_w2scene_repo_path = bpy.props.StringProperty(default="")
    bpy.types.Scene.witcher_w2scene_summary = bpy.props.StringProperty(default="")
    bpy.types.Scene.witcher_w2scene_active_cutscene_path = bpy.props.StringProperty(default="")
    bpy.types.Scene.witcher_w2scene_root_fields = bpy.props.CollectionProperty(type=W2SceneFieldItem)
    bpy.types.Scene.witcher_w2scene_section_fields = bpy.props.CollectionProperty(type=W2SceneFieldItem)
    bpy.types.Scene.witcher_w2scene_actor_items = bpy.props.CollectionProperty(type=W2SceneActorItem)
    bpy.types.Scene.witcher_w2scene_dialogset_items = bpy.props.CollectionProperty(type=W2SceneDialogsetItem)
    bpy.types.Scene.witcher_w2scene_dialogset_slot_items = bpy.props.CollectionProperty(type=W2SceneDialogsetSlotItem)
    bpy.types.Scene.witcher_w2scene_camera_items = bpy.props.CollectionProperty(type=W2SceneCameraItem)
    bpy.types.Scene.witcher_w2scene_section_element_items = bpy.props.CollectionProperty(type=W2SceneSectionElementItem)
    bpy.types.Scene.witcher_w2scene_section_event_items = bpy.props.CollectionProperty(type=W2SceneSectionEventItem)
    bpy.types.Scene.witcher_w2scene_actor_index = bpy.props.IntProperty(default=0)
    bpy.types.Scene.witcher_w2scene_dialogset_index = bpy.props.IntProperty(default=0)
    bpy.types.Scene.witcher_w2scene_dialogset_slot_index = bpy.props.IntProperty(default=0)
    bpy.types.Scene.witcher_w2scene_camera_index = bpy.props.IntProperty(default=0)
    bpy.types.Scene.witcher_w2scene_element_index = bpy.props.IntProperty(default=0)
    bpy.types.Scene.witcher_w2scene_event_index = bpy.props.IntProperty(default=0)
    bpy.types.Scene.witcher_w2scene_show_unset_fields = bpy.props.BoolProperty(name="Show Unset", default=False)
    bpy.types.Scene.witcher_w2scene_write_profile_log = bpy.props.BoolProperty(
        name="Write Scene Profile Log",
        description="Write a timestamped .w2scene section import profile log to the extension cache log folder",
        default=True,
    )
    bpy.types.Scene.witcher_w2scene_create_debug_markers = bpy.props.BoolProperty(
        name="Create Scene Debug Markers",
        description="Create visible .w2scene dialogset and placement marker empties while loading a section",
        default=True,
    )
    bpy.types.Scene.witcher_w2scene_tab = bpy.props.EnumProperty(
        name="Scene Tab",
        items=[
            ('SCENE', 'Scene', 'Scene-level fields and file summary'),
            ('SECTIONS', 'Sections', 'Sections, ordered elements and section events'),
            ('ACTORS', 'Actors', 'Scene actor templates and appearances'),
            ('DIALOGSETS', 'Dialogsets', 'Dialogset instances and slots'),
            ('CAMERAS', 'Cameras', 'Camera definitions embedded in the scene'),
        ],
        default='SECTIONS',
    )
    bpy.types.WindowManager.witcher_dialogset_active_slot_key = bpy.props.StringProperty(default="", options={'SKIP_SAVE'})
    bpy.types.WindowManager.witcher_dialogset_status = bpy.props.EnumProperty(
        name="Status",
        description="Actor status from scene_body_animations.csv",
        items=_dialogset_status_items,
    )
    bpy.types.WindowManager.witcher_dialogset_emotional_state = bpy.props.EnumProperty(
        name="Emotional State",
        description="Actor emotional state filtered by status",
        items=_dialogset_emotional_items,
    )
    bpy.types.WindowManager.witcher_dialogset_pose_name = bpy.props.EnumProperty(
        name="Pose",
        description="Actor body pose filtered by status and emotional state",
        items=_dialogset_pose_items,
    )
    bpy.types.WindowManager.witcher_dialogset_mimics_state = bpy.props.EnumProperty(
        name="Mimics State",
        description="Actor mimics emotional state from scene_mimics_emotional_states.csv",
        items=_dialogset_mimics_state_items,
    )
    bpy.types.WindowManager.witcher_dialogset_mimics_layer_eyes = bpy.props.EnumProperty(
        name="Layer Eyes",
        description="Per-layer eyes override resolved through scene_mimics_emotional_states.csv",
        items=_dialogset_mimics_layer_items,
    )
    bpy.types.WindowManager.witcher_dialogset_mimics_layer_pose = bpy.props.EnumProperty(
        name="Layer Pose",
        description="Per-layer pose override resolved through scene_mimics_emotional_states.csv",
        items=_dialogset_mimics_layer_items,
    )
    bpy.types.WindowManager.witcher_dialogset_mimics_layer_animation = bpy.props.EnumProperty(
        name="Layer Animation",
        description="Per-layer animation override resolved through scene_mimics_emotional_states.csv",
        items=_dialogset_mimics_layer_items,
    )
    bpy.types.WindowManager.witcher_dialogset_mimics_layer_pose_weight = bpy.props.FloatProperty(
        name="Layer Pose Weight",
        description="Blend weight for the mimics pose layer",
        default=1.0,
        min=0.0,
        max=1.0,
        update=_on_dialogset_slot_mimics_pose_weight_changed,
    )
    bpy.types.WindowManager.witcher_dialogset_force_body_idle_animation = bpy.props.StringProperty(
        name="Force Body Idle",
        description="Override animation name for the body idle; bypasses CSV lookup",
        default="",
    )
    bpy.types.WindowManager.witcher_dialogset_force_body_idle_animation_weight = bpy.props.FloatProperty(
        name="Force Body Idle Weight",
        description="Blend weight for the forced body idle",
        default=1.0,
        min=0.0,
        max=1.0,
    )

def unregister():
    if hasattr(bpy.types.Scene, "witcher_sections"):
        del bpy.types.Scene.witcher_sections
    if hasattr(bpy.types.Scene, "witcher_sections_index"):
        del bpy.types.Scene.witcher_sections_index
    if hasattr(bpy.types.Scene, "witcher_sections_filepath"):
        del bpy.types.Scene.witcher_sections_filepath
    for prop in (
        "witcher_loaded_w2scene_name",
        "witcher_loaded_w2scene_path",
        "witcher_w2scene_repo_path",
        "witcher_w2scene_summary",
        "witcher_w2scene_active_cutscene_path",
        "witcher_w2scene_root_fields",
        "witcher_w2scene_section_fields",
        "witcher_w2scene_actor_items",
        "witcher_w2scene_dialogset_items",
        "witcher_w2scene_dialogset_slot_items",
        "witcher_w2scene_camera_items",
        "witcher_w2scene_section_element_items",
        "witcher_w2scene_section_event_items",
        "witcher_w2scene_actor_index",
        "witcher_w2scene_dialogset_index",
        "witcher_w2scene_dialogset_slot_index",
        "witcher_w2scene_camera_index",
        "witcher_w2scene_element_index",
        "witcher_w2scene_event_index",
        "witcher_w2scene_show_unset_fields",
        "witcher_w2scene_write_profile_log",
        "witcher_w2scene_create_debug_markers",
        "witcher_w2scene_tab",
    ):
        if hasattr(bpy.types.Scene, prop):
            delattr(bpy.types.Scene, prop)
    for wm_prop in (
        "witcher_dialogset_active_slot_key",
        "witcher_dialogset_status",
        "witcher_dialogset_emotional_state",
        "witcher_dialogset_pose_name",
        "witcher_dialogset_mimics_state",
        "witcher_dialogset_mimics_layer_eyes",
        "witcher_dialogset_mimics_layer_pose",
        "witcher_dialogset_mimics_layer_animation",
        "witcher_dialogset_mimics_layer_pose_weight",
        "witcher_dialogset_force_body_idle_animation",
        "witcher_dialogset_force_body_idle_animation_weight",
    ):
        if hasattr(bpy.types.WindowManager, wm_prop):
            delattr(bpy.types.WindowManager, wm_prop)
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
