
import logging
import os
log = logging.getLogger(__name__)

from ..CR2W import w3_types
from ..importers import import_cutscene
from ..importers import import_scene

import bpy
from bpy.types import Panel, Operator, UIList, PropertyGroup
from bpy.props import IntProperty, StringProperty, CollectionProperty, FloatProperty, BoolProperty
from bpy_extras.io_utils import ImportHelper

from .. import get_uncook_path

_CUTSCENE_SYNC_DEFERRED = set()

def add_scene_section(name, json_data, scene):
    if not hasattr(scene, "witcher_sections"):
        scene["witcher_sections"] = []
    
    section = scene.witcher_sections.add()
    section.name = name
    section.json_data = json_data

class WitcherSection(bpy.types.PropertyGroup):
    name = StringProperty(name="Name")
    json_data = StringProperty(name="JSON Data")

class CutsceneActorPreviewItem(PropertyGroup):
    source_index: IntProperty(default=-1)
    label: StringProperty(default="")
    actor_name: StringProperty(default="")
    template_path: StringProperty(default="")
    appearance_name: StringProperty(default="")
    actor_type: StringProperty(default="")
    use_mimic: BoolProperty(default=False)
    already_in_scene: BoolProperty(default=False)
    selected: BoolProperty(name="Import", default=True)

class CutsceneAnimationPreviewItem(PropertyGroup):
    source_index: IntProperty(default=-1)
    full_name: StringProperty(default="")
    display_name: StringProperty(default="")
    actor_name: StringProperty(default="")
    component_name: StringProperty(default="")
    frames_per_second: FloatProperty(default=0.0)
    num_frames: IntProperty(default=0)
    duration: FloatProperty(default=0.0)
    selected: BoolProperty(name="Import", default=True)

class CutsceneLoadedActorItem(PropertyGroup):
    source_index: IntProperty(default=-1)
    label: StringProperty(default="")
    actor_name: StringProperty(default="")
    voice_tag: StringProperty(default="")
    template_path: StringProperty(default="")
    appearance_name: StringProperty(default="")
    actor_type: StringProperty(default="")
    use_mimic: BoolProperty(default=False)
    object_name: StringProperty(default="")
    cutscene_guid: StringProperty(default="")
    is_loaded: BoolProperty(default=False)
    imported_by_cutscene: BoolProperty(default=False)

class CutsceneLoadedAnimationItem(PropertyGroup):
    source_index: IntProperty(default=-1)
    full_name: StringProperty(default="")
    display_name: StringProperty(default="")
    actor_name: StringProperty(default="")
    component_name: StringProperty(default="")
    frames_per_second: FloatProperty(default=0.0)
    num_frames: IntProperty(default=0)
    duration: FloatProperty(default=0.0)
    is_loaded: BoolProperty(default=False)


class CutsceneEventItem(PropertyGroup):
    event_type: StringProperty(default="")       # e.g. "CExtAnimCutsceneDialogEvent"
    event_name: StringProperty(name="Event Name", default="")
    start_time: FloatProperty(name="Start Time", default=0.0)
    duration: FloatProperty(name="Duration", default=0.0)
    animation_name: StringProperty(name="Animation", default="")
    track_name: StringProperty(name="Track", default="")
    effect_name: StringProperty(name="Effect", default="")
    appearance: StringProperty(name="Appearance", default="")
    event_scope: StringProperty(default="ROOT")
    source_index: IntProperty(default=-1)

class CutsceneEffectItem(PropertyGroup):
    name: StringProperty(default="")

class CutsceneTemplateFieldItem(PropertyGroup):
    class_name: StringProperty(default="")
    field_name: StringProperty(default="")
    value_text: StringProperty(default="")
    is_set: BoolProperty(default=False)

class CutsceneDialogItem(PropertyGroup):
    actor: StringProperty(default="")
    voice_file: StringProperty(default="")
    sound_event: StringProperty(default="")
    line_index: IntProperty(default=0)
    scene_path: StringProperty(default="")


_IMPORTED_FIELD_LIST_LIMIT = 6


def _get_present_imported_fields(imported_data):
    return {
        str(field_name or "").strip()
        for field_name in (
            getattr(imported_data, "presentPropertyNames", None)
            or getattr(imported_data, "presentTemplateProps", None)
            or set()
        )
        if str(field_name or "").strip()
    }


def _get_imported_field_schema(imported_data, fallback_schema=()):
    schema = getattr(imported_data, "importedClassFieldSchema", None) if imported_data is not None else None
    return schema or fallback_schema


def _get_imported_field_value(imported_data, field_name):
    if imported_data is None:
        return None
    return getattr(imported_data, field_name, None)


def _get_imported_value_label(value):
    if value is None:
        return ""

    if isinstance(value, dict):
        for key in ("name", "Name", "$type"):
            text = str(value.get(key) or "").strip()
            if text:
                return text
        return ""

    animation = getattr(value, "animation", None)
    if animation is not None:
        text = str(getattr(animation, "name", "") or "").strip()
        if text:
            return text

    for attr_name in ("name", "template", "type_name"):
        text = str(getattr(value, attr_name, "") or "").strip()
        if text:
            return text

    return ""


def _format_imported_field_value(value, depth=0):
    if value is None:
        return "\"\""

    if isinstance(value, bool):
        return "True" if bool(value) else "False"

    if isinstance(value, (int, float)):
        return f"{float(value):g}" if isinstance(value, float) else str(value)

    if isinstance(value, str):
        return value if value else "\"\""

    if isinstance(value, dict):
        if not value:
            return "{}"
        parts = []
        items = list(value.items())
        for key, item_value in items[:_IMPORTED_FIELD_LIST_LIMIT]:
            parts.append(f"{key}={_format_imported_field_value(item_value, depth + 1)}")
        text = ", ".join(parts) if parts else "{}"
        if len(items) > _IMPORTED_FIELD_LIST_LIMIT:
            text += f" (+{len(items) - _IMPORTED_FIELD_LIST_LIMIT} more)"
        return text

    if isinstance(value, (list, tuple, set)):
        seq = list(value)
        if not seq:
            return "[]"
        items = []
        for item in seq[:_IMPORTED_FIELD_LIST_LIMIT]:
            label = _get_imported_value_label(item)
            items.append(label or _format_imported_field_value(item, depth + 1))
        text = ", ".join(item for item in items if item)
        if len(seq) > _IMPORTED_FIELD_LIST_LIMIT:
            text += f" (+{len(seq) - _IMPORTED_FIELD_LIST_LIMIT} more)"
        return text or "[]"

    label = _get_imported_value_label(value)
    if label:
        return label

    text = str(value or "").strip()
    return text if text else "\"\""


def _sync_cutscene_template_fields(scene, cutscene):
    scene.witcher_cutscene_template_fields.clear()
    if cutscene is None:
        return

    schema = _get_imported_field_schema(cutscene, fallback_schema=w3_types.CUTSCENE_CLASS_FIELD_SCHEMA)
    present_fields = _get_present_imported_fields(cutscene)
    for class_name, fields in schema:
        for field_name, _default in fields:
            item = scene.witcher_cutscene_template_fields.add()
            item.class_name = class_name
            item.field_name = field_name
            item.is_set = field_name in present_fields
            if item.is_set:
                value = _get_imported_field_value(cutscene, field_name)
                item.value_text = _format_imported_field_value(value)
            else:
                item.value_text = "<unset>"

class WITCH_UL_CutsceneActorPreview(UIList):
    bl_idname = "WITCH_UL_CutsceneActorPreview"
    layout_type = "DEFAULT"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index, flt_flag):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            row.prop(item, "selected", text="")
            row.label(text=item.label or item.actor_name or "Actor", icon='ARMATURE_DATA')
            if item.already_in_scene:
                row.label(text="IN SCENE", icon='CHECKMARK')
            if item.appearance_name:
                row.label(text=item.appearance_name, icon='MATERIAL_DATA')
        elif self.layout_type in {'GRID'}:
            layout.alignment = 'CENTER'
            layout.label(text="")

class WITCH_UL_CutsceneAnimationPreview(UIList):
    bl_idname = "WITCH_UL_CutsceneAnimationPreview"
    layout_type = "DEFAULT"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index, flt_flag):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            row.prop(item, "selected", text="")
            row.label(text=_get_cutscene_animation_label(item), icon='ACTION')
            if item.component_name:
                row.label(text=item.component_name, icon='BONE_DATA')
        elif self.layout_type in {'GRID'}:
            layout.alignment = 'CENTER'
            layout.label(text="")


def _event_type_icon(event_type):
    if 'BodyPart' in event_type or 'Appearance' in event_type:
        return 'MATERIAL_DATA'
    if 'Dialog' in event_type or 'Lookat' in event_type:
        return 'OUTLINER_OB_SPEAKER'
    if 'Effect' in event_type or 'Fx' in event_type:
        return 'SHADERFX'
    if 'Sound' in event_type:
        return 'SOUND'
    if 'Fade' in event_type:
        return 'IMAGE_ALPHA'
    if 'Slow' in event_type or 'Wind' in event_type or 'Environment' in event_type:
        return 'WORLD'
    if 'Break' in event_type:
        return 'CANCEL'
    return 'KEYFRAME'


def _get_cutscene_event_label(item):
    event_type = str(getattr(item, "event_type", "") or "")
    appearance = str(getattr(item, "appearance", "") or "").strip()
    if appearance and "BodyPartEvent" in event_type:
        return appearance

    effect_name = str(getattr(item, "effect_name", "") or "").strip()
    if effect_name and ("Effect" in event_type or "Fx" in event_type):
        return effect_name

    event_name = str(getattr(item, "event_name", "") or "").strip()
    return event_name or event_type or "Event"




class WITCH_UL_CutsceneDialogList(UIList):
    bl_idname = "WITCH_UL_CutsceneDialogList"
    layout_type = "DEFAULT"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index, flt_flag):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            row.label(text=item.actor or "?", icon='OUTLINER_OB_SPEAKER')
            row.label(text=item.voice_file or item.sound_event or "")
            if item.line_index:
                row.label(text=str(item.line_index))
        elif self.layout_type in {'GRID'}:
            layout.alignment = 'CENTER'
            layout.label(text="")


class WITCH_UL_LoadedActorList(UIList):
    bl_idname = "WITCH_UL_LoadedActorList"
    layout_type = "DEFAULT"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index, flt_flag):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            state = _get_cutscene_actor_display_state(item)
            row.label(text="", icon='CHECKMARK' if state["is_loaded"] else 'RADIOBUT_OFF')
            label = item.label or item.actor_name or f"Actor {item.source_index + 1}"
            row.label(text=label, icon='ARMATURE_DATA')
            if item.appearance_name:
                row.label(text=item.appearance_name, icon='MATERIAL_DATA')
            atype = str(item.actor_type or "").replace("CAT_", "")
            if atype and atype != "Actor":
                row.label(text=atype)
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text="")


class WITCH_UL_LoadedAnimList(UIList):
    bl_idname = "WITCH_UL_LoadedAnimList"
    layout_type = "DEFAULT"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index, flt_flag):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            if item.source_index == -1:
                row.label(text="Cutscene", icon='SCENE_DATA')
            else:
                row.label(text="", icon='CHECKMARK' if item.is_loaded else 'RADIOBUT_OFF')
                row.label(text=_get_cutscene_animation_label(item), icon='ACTION')
                if item.duration:
                    row.label(text=f"{item.duration:.2f}s")
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text="")


def _find_actor_obj_by_voicetag(scene, voicetag):
    """Return the Blender armature object for the actor whose voiceTag matches."""
    tag_lower = str(voicetag or "").lower().strip()
    if not tag_lower:
        return None
    for actor_item in getattr(scene, "witcher_cutscene_actor_items", []):
        # Primary: match against the stored voiceTag
        if str(getattr(actor_item, "voice_tag", "") or "").lower().strip() == tag_lower:
            return _get_loaded_cutscene_actor_object(actor_item)
        # Fallback: actor_name (cutscene slot name) often equals voiceTag case-insensitively
        if str(getattr(actor_item, "actor_name", "") or "").lower().strip() == tag_lower:
            return _get_loaded_cutscene_actor_object(actor_item)
    return None


class WITCH_OT_LoadCutsceneDialogs(bpy.types.Operator):
    bl_idname = "witcher.load_cutscene_dialogs"
    bl_label = "Load Dialogs"
    bl_description = (
        "Reverse-lookup dialog lines from the linked .w2scene, then load each voice "
        "line + lipsync onto the matching actor at the time given by the cutscene's "
        "CExtAnimCutsceneDialogEvent markers"
    )

    def execute(self, context):
        from ..ui.ui_voice import load_voice_and_lipsync

        scene = context.scene
        filepath = str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or "").strip()
        if not filepath:
            self.report({'WARNING'}, "No cutscene loaded.")
            return {'CANCELLED'}

        # --- 1. Load dialog lines from the linked .w2scene --------------------
        scene.witcher_cutscene_dialog_items.clear()
        try:
            dialog_items = import_cutscene.load_cutscene_dialog_items(filepath)
        except Exception as exc:
            log.exception("Failed to load dialog items for %s", filepath)
            self.report({'ERROR'}, f"Dialog load failed: {exc}")
            return {'CANCELLED'}

        for d in dialog_items:
            item = scene.witcher_cutscene_dialog_items.add()
            item.actor = str(d.get("actor", "") or "")
            item.voice_file = str(d.get("voice_file", "") or "")
            item.sound_event = str(d.get("sound_event", "") or "")
            item.line_index = int(d.get("line_index", 0) or 0)
            item.scene_path = str(d.get("scene_path", "") or "")

        if not dialog_items:
            self.report({'INFO'}, "No dialog lines found in linked .w2scene.")
            return {'FINISHED'}

        # --- 2. Get ordered dialog events for timing --------------------------
        # CExtAnimCutsceneDialogEvent root events, sorted by start_time.
        # The nth event gives the start_time for the nth dialog line (engine rule).
        all_events = list(getattr(scene, "witcher_cutscene_event_items", []))
        dialog_events = sorted(
            [e for e in all_events
             if "DialogEvent" in str(getattr(e, "event_type", "") or "")
             and str(getattr(e, "event_scope", "") or "").upper() == "ROOT"],
            key=lambda e: float(getattr(e, "start_time", 0.0) or 0.0),
        )

        fps = float(scene.render.fps)

        # --- 3. Load each line ------------------------------------------------
        loaded = 0
        skipped = 0
        for idx, d in enumerate(dialog_items):
            line_index = int(d.get("line_index", 0) or 0)
            if not line_index:
                skipped += 1
                continue

            voicetag = str(d.get("actor", "") or "")
            actor_obj = _find_actor_obj_by_voicetag(scene, voicetag)

            # Timing: use nth dialog event's start_time; fall back to 0 if fewer events than lines
            at_frame = 0.0
            if idx < len(dialog_events):
                at_frame = float(getattr(dialog_events[idx], "start_time", 0.0) or 0.0) * fps

            try:
                load_voice_and_lipsync(
                    str(line_index),
                    actor=actor_obj,
                    context=context,
                    at_frame=at_frame,
                )
                loaded += 1
            except Exception as exc:
                log.warning("Failed to load voice line %s for actor %s: %s", line_index, voicetag, exc)
                skipped += 1

        msg = f"Loaded {loaded} voice line(s)"
        if skipped:
            msg += f" ({skipped} skipped)"
        self.report({'INFO'}, msg)
        return {'FINISHED'}


def _get_cutscene_animation_label(animation_entry):
    if animation_entry is None:
        return "Animation"
    full_name = str(getattr(animation_entry, "full_name", "") or "").strip()
    if full_name:
        return full_name
    display_name = str(getattr(animation_entry, "display_name", "") or "").strip()
    actor_name = str(getattr(animation_entry, "actor_name", "") or "").strip()
    if actor_name and display_name:
        return f"{actor_name}: {display_name}"
    return display_name or "Animation"

def _clear_cutscene_preview(operator):
    operator.cutscene_actor_items.clear()
    operator.cutscene_animation_items.clear()
    operator.cutscene_actor_index = 0
    operator.cutscene_animation_index = 0

def _cutscene_actor_preview_key(item):
    return (
        int(getattr(item, "source_index", -1)),
        str(getattr(item, "actor_name", "") or ""),
        str(getattr(item, "template_path", "") or ""),
        str(getattr(item, "appearance_name", "") or ""),
    )

def _cutscene_animation_preview_key(item):
    return (
        int(getattr(item, "source_index", -1)),
        str(getattr(item, "full_name", "") or ""),
        str(getattr(item, "actor_name", "") or ""),
        str(getattr(item, "component_name", "") or ""),
    )

def _update_cutscene_preview(operator):
    filepath = str(getattr(operator, "filepath", "") or "").strip()
    if not filepath or os.path.isdir(filepath):
        _clear_cutscene_preview(operator)
        operator.cutscene_preview_path = filepath
        operator.cutscene_preview_mtime = 0.0
        operator.cutscene_preview_status = "Select a .w2cutscene file"
        return True

    lowered = filepath.lower()
    if not lowered.endswith(".w2cutscene"):
        _clear_cutscene_preview(operator)
        operator.cutscene_preview_path = filepath
        operator.cutscene_preview_mtime = 0.0
        operator.cutscene_preview_status = "Unsupported file type"
        return True

    try:
        mtime = os.path.getmtime(filepath)
    except OSError:
        _clear_cutscene_preview(operator)
        operator.cutscene_preview_path = filepath
        operator.cutscene_preview_mtime = 0.0
        operator.cutscene_preview_status = "File not found"
        return True

    if (
        operator.cutscene_preview_path == filepath
        and abs(operator.cutscene_preview_mtime - mtime) < 0.0001
        and (operator.cutscene_actor_items or operator.cutscene_animation_items)
    ):
        return False

    old_actor_selection = {
        _cutscene_actor_preview_key(item): bool(item.selected)
        for item in operator.cutscene_actor_items
    }
    old_animation_selection = {
        _cutscene_animation_preview_key(item): bool(item.selected)
        for item in operator.cutscene_animation_items
    }
    old_actor_index = int(getattr(operator, "cutscene_actor_index", 0) or 0)
    old_animation_index = int(getattr(operator, "cutscene_animation_index", 0) or 0)

    _clear_cutscene_preview(operator)
    operator.cutscene_preview_path = filepath
    operator.cutscene_preview_mtime = mtime

    try:
        _cutscene, actor_items, animation_items, event_items = import_cutscene.collect_cutscene_preview(filepath)
    except Exception as exc:
        log.exception("Failed to build cutscene preview for %s", filepath)
        operator.cutscene_preview_status = f"Preview error: {exc}"
        return True

    if not actor_items and not animation_items:
        operator.cutscene_preview_status = "No actors or animations found in file"
        return True

    for actor_data in actor_items:
        item = operator.cutscene_actor_items.add()
        item.source_index = int(actor_data["source_index"])
        item.label = str(actor_data["label"])
        item.actor_name = str(actor_data["actor_name"])
        item.template_path = str(actor_data["template_path"])
        item.appearance_name = str(actor_data["appearance_name"])
        item.actor_type = str(actor_data["actor_type"])
        item.use_mimic = bool(actor_data["use_mimic"])
        item.already_in_scene = bool(actor_data["already_in_scene"])
        actor_key = _cutscene_actor_preview_key(item)
        item.selected = old_actor_selection.get(actor_key, True)

    for animation_data in animation_items:
        item = operator.cutscene_animation_items.add()
        item.source_index = int(animation_data["source_index"])
        item.full_name = str(animation_data["full_name"])
        item.display_name = str(animation_data["display_name"])
        item.actor_name = str(animation_data["actor_name"])
        item.component_name = str(animation_data["component_name"])
        item.frames_per_second = float(animation_data["frames_per_second"])
        item.num_frames = int(animation_data["num_frames"])
        item.duration = float(animation_data["duration"])
        animation_key = _cutscene_animation_preview_key(item)
        item.selected = old_animation_selection.get(animation_key, True)

    if operator.cutscene_actor_items:
        operator.cutscene_actor_index = min(max(0, old_actor_index), len(operator.cutscene_actor_items) - 1)
    else:
        operator.cutscene_actor_index = 0

    if operator.cutscene_animation_items:
        operator.cutscene_animation_index = min(max(0, old_animation_index), len(operator.cutscene_animation_items) - 1)
    else:
        operator.cutscene_animation_index = 0

    event_suffix = f", {len(event_items)} event(s)" if event_items else ""
    operator.cutscene_preview_status = (
        f"{len(actor_items)} actor(s), {len(animation_items)} animation(s){event_suffix} found"
    )
    return True

def _clear_loaded_cutscene_state(scene):
    scene.witcher_cutscene_actor_items.clear()
    scene.witcher_cutscene_animation_items.clear()
    scene.witcher_cutscene_event_items.clear()
    scene.witcher_cutscene_template_fields.clear()
    scene.witcher_cutscene_effect_items.clear()
    scene.witcher_cutscene_dialog_items.clear()
    scene.witcher_loaded_cutscene_name = ""
    if hasattr(scene, "witcher_cutscene_last_import_seconds"):
        scene.witcher_cutscene_last_import_seconds = 0.0
    if hasattr(scene, "witcher_loaded_w2cutscene_path"):
        scene.witcher_loaded_w2cutscene_path = ""

def _schedule_deferred_cutscene_state_sync(scene, filepath):
    scene_name = str(getattr(scene, "name", "") or "").strip()
    filepath = str(filepath or "").strip()
    if not scene_name or not filepath:
        return
    key = (scene_name, filepath)
    if key in _CUTSCENE_SYNC_DEFERRED:
        return
    _CUTSCENE_SYNC_DEFERRED.add(key)

    def _do_sync():
        _CUTSCENE_SYNC_DEFERRED.discard(key)
        target_scene = bpy.data.scenes.get(scene_name)
        if target_scene is None:
            return None
        try:
            _sync_loaded_cutscene_state(target_scene, filepath)
        except Exception:
            log.exception("Failed deferred cutscene state sync for %s", filepath)
        return None

    try:
        bpy.app.timers.register(_do_sync, first_interval=0.0)
    except Exception:
        _CUTSCENE_SYNC_DEFERRED.discard(key)

def _get_loaded_cutscene_name(filepath):
    filepath = str(filepath or "").strip()
    if not filepath:
        return ""
    return os.path.basename(filepath)

def _find_loaded_cutscene_actor_entry(scene, source_index):
    try:
        source_index = int(source_index)
    except Exception:
        source_index = -1
    for item in getattr(scene, "witcher_cutscene_actor_items", []):
        if int(getattr(item, "source_index", -1)) == source_index:
            return item
    return None

def _find_loaded_cutscene_animation_entry(scene, source_index):
    try:
        source_index = int(source_index)
    except Exception:
        source_index = -1
    for item in getattr(scene, "witcher_cutscene_animation_items", []):
        if int(getattr(item, "source_index", -1)) == source_index:
            return item
    return None

def _get_loaded_cutscene_actor_object(actor_entry):
    if actor_entry is None:
        return None
    object_name = str(getattr(actor_entry, "object_name", "") or "").strip()
    if not object_name:
        return None
    obj = bpy.data.objects.get(object_name)
    if obj is None or getattr(obj, "type", None) != 'ARMATURE':
        return None
    return obj

def _animation_matches_actor_entry(scene, animation_entry, actor_entry):
    if animation_entry is None or actor_entry is None:
        return False
    actor_name = str(getattr(actor_entry, "actor_name", "") or "").strip()
    animation_actor_name = str(getattr(animation_entry, "actor_name", "") or "").strip()
    if actor_name and animation_actor_name:
        return actor_name == animation_actor_name
    if animation_actor_name and not actor_name:
        return False
    actor_entries = list(getattr(scene, "witcher_cutscene_actor_items", []))
    if len(actor_entries) == 1:
        return actor_entries[0].source_index == actor_entry.source_index
    return False

def _find_actor_entry_for_animation(scene, animation_entry):
    if animation_entry is None:
        return None
    animation_actor_name = str(getattr(animation_entry, "actor_name", "") or "").strip()
    if animation_actor_name:
        for actor_entry in getattr(scene, "witcher_cutscene_actor_items", []):
            if str(getattr(actor_entry, "actor_name", "") or "").strip() == animation_actor_name:
                return actor_entry
    actor_entries = list(getattr(scene, "witcher_cutscene_actor_items", []))
    if len(actor_entries) == 1:
        return actor_entries[0]
    return None

def _validate_loaded_cutscene_state(scene):
    filepath = str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or "").strip()
    for actor_entry in getattr(scene, "witcher_cutscene_actor_items", []):
        actor_obj = _get_loaded_cutscene_actor_object(actor_entry)
        if actor_obj is None:
            actor_entry.is_loaded = False
            actor_entry.object_name = ""
            actor_entry.cutscene_guid = ""
            actor_entry.imported_by_cutscene = False
        else:
            actor_entry.is_loaded = True
            if not actor_entry.cutscene_guid:
                actor_entry.cutscene_guid = str(actor_obj.get(import_cutscene.CUTSCENE_GUID_PROP, "") or "")
            actor_entry.imported_by_cutscene = bool(actor_obj.get(import_cutscene.CUTSCENE_ACTOR_IMPORTED_PROP, False))

    if not filepath:
        return

    for animation_entry in getattr(scene, "witcher_cutscene_animation_items", []):
        if not animation_entry.is_loaded:
            continue
        actor_entry = _find_actor_entry_for_animation(scene, animation_entry)
        actor_obj = _get_loaded_cutscene_actor_object(actor_entry)
        if actor_obj is None:
            animation_entry.is_loaded = False
            continue
        if not import_cutscene.is_cutscene_animation_loaded(
            actor_obj,
            animation_entry.full_name,
            filepath,
            animation_entry.source_index,
        ):
            animation_entry.is_loaded = False

def _get_cutscene_actor_display_state(actor_entry):
    actor_obj = _get_loaded_cutscene_actor_object(actor_entry)
    is_loaded = actor_obj is not None
    imported_by_cutscene = bool(getattr(actor_entry, "imported_by_cutscene", False))
    cutscene_guid = str(getattr(actor_entry, "cutscene_guid", "") or "")
    if actor_obj is not None:
        imported_by_cutscene = bool(actor_obj.get(import_cutscene.CUTSCENE_ACTOR_IMPORTED_PROP, False))
        cutscene_guid = str(actor_obj.get(import_cutscene.CUTSCENE_GUID_PROP, "") or cutscene_guid)
    return {
        "actor_obj": actor_obj,
        "is_loaded": is_loaded,
        "imported_by_cutscene": imported_by_cutscene,
        "cutscene_guid": cutscene_guid,
    }

def _get_cutscene_animation_display_state(scene, animation_entry):
    actor_entry = _find_actor_entry_for_animation(scene, animation_entry)
    actor_state = _get_cutscene_actor_display_state(actor_entry)
    actor_obj = actor_state["actor_obj"]
    is_loaded = False
    if actor_obj is not None:
        is_loaded = import_cutscene.is_cutscene_animation_loaded(
            actor_obj,
            animation_entry.full_name,
            getattr(scene, "witcher_loaded_w2cutscene_path", ""),
            animation_entry.source_index,
        )
    elif bool(getattr(animation_entry, "is_loaded", False)):
        is_loaded = False
    return {
        "actor_entry": actor_entry,
        "actor_state": actor_state,
        "is_loaded": is_loaded,
    }

def _sync_loaded_cutscene_state(scene, filepath, cutscene_data=None):
    filepath = str(filepath or "").strip()
    if not filepath:
        _clear_loaded_cutscene_state(scene)
        return
    if cutscene_data is not None and hasattr(scene, "witcher_cutscene_last_import_seconds"):
        try:
            scene.witcher_cutscene_last_import_seconds = float(getattr(cutscene_data, "import_duration_seconds", 0.0) or 0.0)
        except Exception:
            pass

    same_path = os.path.normcase(os.path.normpath(str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or ""))) == os.path.normcase(os.path.normpath(filepath))
    old_actor_state = {}
    old_animation_state = {}
    if same_path:
        old_actor_state = {
            int(item.source_index): {
                "object_name": str(item.object_name or ""),
                "cutscene_guid": str(item.cutscene_guid or ""),
                "is_loaded": bool(item.is_loaded),
                "imported_by_cutscene": bool(item.imported_by_cutscene),
            }
            for item in getattr(scene, "witcher_cutscene_actor_items", [])
        }
        old_animation_state = {
            int(item.source_index): bool(item.is_loaded)
            for item in getattr(scene, "witcher_cutscene_animation_items", [])
        }

    _cutscene, actor_items, animation_items, event_items = import_cutscene.collect_cutscene_preview(
        filepath,
        cutscene_template=cutscene_data,
    )

    _sync_cutscene_template_fields(scene, _cutscene)
    scene.witcher_cutscene_effect_items.clear()
    if _cutscene is not None:
        for eff in (getattr(_cutscene, "effects", None) or []):
            ei = scene.witcher_cutscene_effect_items.add()
            if isinstance(eff, dict):
                ei.name = str(eff.get("name") or eff.get("Name") or eff.get("$type", "CFXDefinition"))
            elif hasattr(eff, "name") and eff.name:
                ei.name = str(eff.name)
            else:
                ei.name = "CFXDefinition"

    loaded_actor_object_names = dict(getattr(cutscene_data, "loaded_actor_object_names_by_index", {}) or {})
    loaded_actor_imported_flags = dict(getattr(cutscene_data, "loaded_actor_imported_flags_by_index", {}) or {})
    loaded_actor_guid_by_index = dict(getattr(cutscene_data, "loaded_actor_guid_by_index", {}) or {})
    applied_animation_indices = {
        int(idx)
        for idx in (getattr(cutscene_data, "applied_animation_indices", []) or [])
    }

    scene.witcher_cutscene_actor_items.clear()
    scene.witcher_cutscene_animation_items.clear()
    scene.witcher_cutscene_event_items.clear()
    scene.witcher_cutscene_dialog_items.clear()
    scene.witcher_loaded_cutscene_name = _get_loaded_cutscene_name(filepath)
    scene.witcher_loaded_w2cutscene_path = filepath

    for actor_data in actor_items:
        source_index = int(actor_data["source_index"])
        state = dict(old_actor_state.get(source_index, {}))
        item = scene.witcher_cutscene_actor_items.add()
        item.source_index = source_index
        item.label = str(actor_data["label"])
        item.actor_name = str(actor_data["actor_name"])
        item.voice_tag = str(actor_data.get("voice_tag", "") or "")
        item.template_path = str(actor_data["template_path"])
        item.appearance_name = str(actor_data["appearance_name"])
        item.actor_type = str(actor_data["actor_type"])
        item.use_mimic = bool(actor_data["use_mimic"])
        item.object_name = str(state.get("object_name", "") or "")
        item.cutscene_guid = str(state.get("cutscene_guid", "") or "")
        item.is_loaded = bool(state.get("is_loaded", False))
        item.imported_by_cutscene = bool(state.get("imported_by_cutscene", False))
        if source_index in loaded_actor_object_names:
            item.object_name = str(loaded_actor_object_names[source_index] or "")
            item.cutscene_guid = str(loaded_actor_guid_by_index.get(source_index, "") or "")
            item.is_loaded = bool(item.object_name)
            item.imported_by_cutscene = bool(loaded_actor_imported_flags.get(source_index, False))

    # Index 0 is always the "Cutscene" sentinel (root events)
    cutscene_root_item = scene.witcher_cutscene_animation_items.add()
    cutscene_root_item.source_index = -1
    cutscene_root_item.full_name = "Cutscene"
    cutscene_root_item.display_name = "Cutscene"

    for animation_data in animation_items:
        source_index = int(animation_data["source_index"])
        item = scene.witcher_cutscene_animation_items.add()
        item.source_index = source_index
        item.full_name = str(animation_data["full_name"])
        item.display_name = str(animation_data["display_name"])
        item.actor_name = str(animation_data["actor_name"])
        item.component_name = str(animation_data["component_name"])
        item.frames_per_second = float(animation_data["frames_per_second"])
        item.num_frames = int(animation_data["num_frames"])
        item.duration = float(animation_data["duration"])
        item.is_loaded = bool(old_animation_state.get(source_index, False))
        if source_index in applied_animation_indices:
            item.is_loaded = True

    for event_data in event_items:
        item = scene.witcher_cutscene_event_items.add()
        item.event_type = str(event_data["event_type"])
        item.event_name = str(event_data["event_name"])
        item.start_time = float(event_data["start_time"])
        item.duration = float(event_data["duration"])
        item.animation_name = str(event_data["animation_name"])
        item.track_name = str(event_data["track_name"])
        item.effect_name = str(event_data["effect_name"])
        item.appearance = str(event_data.get("appearance", "") or "")
        item.event_scope = str(event_data.get("event_scope", "ROOT"))
        item.source_index = int(event_data.get("source_index", -1))

    _validate_loaded_cutscene_state(scene)

def _update_loaded_actor_entry_from_result(actor_entry, actor_info):
    if actor_entry is None or not actor_info:
        return
    actor_obj = actor_info.get("actor_obj")
    actor_entry.object_name = str(getattr(actor_obj, "name", "") or "")
    actor_entry.cutscene_guid = str(actor_info.get("cutscene_guid", "") or "")
    actor_entry.is_loaded = bool(actor_obj)
    actor_entry.imported_by_cutscene = bool(actor_info.get("imported_new", False))

def _load_cutscene_actor_entry(scene, actor_entry):
    filepath = str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or "").strip()
    if not filepath or actor_entry is None:
        return None
    actor_info = import_cutscene.load_cutscene_actor(filepath, actor_entry.source_index)
    _update_loaded_actor_entry_from_result(actor_entry, actor_info)
    return actor_info.get("actor_obj") if actor_info else None

def _rebuild_cutscene_actor_animations(scene, actor_entry):
    if actor_entry is None:
        return set(), {}
    actor_obj = _get_loaded_cutscene_actor_object(actor_entry)
    if actor_obj is None:
        for animation_entry in getattr(scene, "witcher_cutscene_animation_items", []):
            if _animation_matches_actor_entry(scene, animation_entry, actor_entry):
                animation_entry.is_loaded = False
        return set(), {}

    filepath = str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or "").strip()
    if not filepath:
        return set(), {}

    animation_indices = [
        int(animation_entry.source_index)
        for animation_entry in getattr(scene, "witcher_cutscene_animation_items", [])
        if bool(getattr(animation_entry, "is_loaded", False))
        and _animation_matches_actor_entry(scene, animation_entry, actor_entry)
    ]

    import_cutscene.clear_cutscene_actor_animation_tracks(actor_obj)
    if not animation_indices:
        return set(), {}

    applied_indices, error_messages = import_cutscene.apply_cutscene_animation_sequence(
        filepath,
        animation_indices,
        actor_obj,
        actor_name=actor_entry.actor_name,
        return_errors=True,
    )
    for animation_entry in getattr(scene, "witcher_cutscene_animation_items", []):
        if _animation_matches_actor_entry(scene, animation_entry, actor_entry):
            animation_entry.is_loaded = int(animation_entry.source_index) in applied_indices
    return applied_indices, error_messages

class ButtonOperatorImportW2scene(bpy.types.Operator, ImportHelper):
    """Import W2 Cutscee"""
    bl_idname = "witcher.import_w2_scene"
    bl_label = "W2 Scene"
    filename_ext = ".w2scene"
    def execute(self, context):
        if os.path.isdir(self.filepath):
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}
        context.scene.witcher_sections_filepath = self.filepath
        context.scene.witcher_sections.clear()
        sceneImporter = import_scene.import_w3_scene(self.filepath)
        sceneImporter.load_sections()
        for section in sceneImporter.scene_sections:
            add_scene_section(section.sectionName, "{}", context.scene)
        #sceneImporter.execute()
        bpy.context.view_layer.update()
        return {'FINISHED'}
    def invoke(self, context, event):
        UNCOOK_PATH = os.path.join(get_uncook_path(context),"animations\\")
        if os.path.exists(UNCOOK_PATH):
            self.filepath = UNCOOK_PATH if self.filepath == '' else self.filepath
        return ImportHelper.invoke(self, context, event)

class ButtonOperatorImportW2cutscene(bpy.types.Operator, ImportHelper):
    """Import W2 Cutscee"""
    bl_idname = "witcher.import_w2_cutscene"
    bl_label = "W2 Cutscene"
    filename_ext = ".w2cutscene"
    bl_options = {'REGISTER', 'UNDO'}

    filter_glob: StringProperty(default='*.w2cutscene', options={'HIDDEN'})
    auto_apply_animations: BoolProperty(
        name="Auto Apply Animations",
        default=True,
        description="Load selected cutscene animations onto their matching actors after import",
    )

    cutscene_actor_items: CollectionProperty(type=CutsceneActorPreviewItem)
    cutscene_actor_index: IntProperty(default=0)
    cutscene_animation_items: CollectionProperty(type=CutsceneAnimationPreviewItem)
    cutscene_animation_index: IntProperty(default=0)
    cutscene_preview_status: StringProperty(default="Select a .w2cutscene file")
    cutscene_preview_path: StringProperty(default="")
    cutscene_preview_mtime: FloatProperty(default=0.0)

    def draw(self, context):
        layout = self.layout

        settings_box = layout.box()
        settings_box.label(text="Import Settings")
        settings_box.prop(self, "auto_apply_animations")

        status_box = layout.box()
        status_box.label(text="Cutscene Preview")
        status_box.label(text=self.cutscene_preview_status)

        actor_box = layout.box()
        actor_box.label(text="Entities", icon='OUTLINER_OB_ARMATURE')
        if self.cutscene_actor_items:
            actor_box.template_list(
                "WITCH_UL_CutsceneActorPreview",
                "",
                self,
                "cutscene_actor_items",
                self,
                "cutscene_actor_index",
                rows=6,
            )
            selected_actor_count = sum(1 for item in self.cutscene_actor_items if item.selected)
            actor_box.label(text=f"Will import/reuse: {selected_actor_count}/{len(self.cutscene_actor_items)} entities")
            idx = self.cutscene_actor_index
            if 0 <= idx < len(self.cutscene_actor_items):
                actor = self.cutscene_actor_items[idx]
                details = actor_box.column(align=True)
                if actor.template_path:
                    details.label(text=f"Template: {actor.template_path}")
                if actor.actor_type:
                    details.label(text=f"Type: {actor.actor_type}")
                if actor.appearance_name:
                    details.label(text=f"Appearance: {actor.appearance_name}")
                if actor.use_mimic:
                    details.label(text="Uses mimic data")

        anim_box = layout.box()
        anim_box.label(text="Animations", icon='ACTION')
        if self.cutscene_animation_items:
            anim_box.template_list(
                "WITCH_UL_CutsceneAnimationPreview",
                "",
                self,
                "cutscene_animation_items",
                self,
                "cutscene_animation_index",
                rows=8,
            )
            selected_animation_count = sum(1 for item in self.cutscene_animation_items if item.selected)
            anim_box.label(text=f"Will import: {selected_animation_count}/{len(self.cutscene_animation_items)} animations")
            if self.auto_apply_animations:
                anim_box.label(text="Auto-apply uses matching actors already in scene or selected for import.", icon='INFO')
            idx = self.cutscene_animation_index
            if 0 <= idx < len(self.cutscene_animation_items):
                anim = self.cutscene_animation_items[idx]
                details = anim_box.column(align=True)
                details.label(text=f"Name: {anim.full_name}")
                if anim.component_name:
                    details.label(text=f"Component: {anim.component_name}")
                if anim.frames_per_second:
                    details.label(text=f"FPS: {anim.frames_per_second:.2f}")
                if anim.num_frames:
                    details.label(text=f"Frames: {anim.num_frames}")
                if anim.duration:
                    details.label(text=f"Duration: {anim.duration:.3f}s")

    def check(self, context):
        return _update_cutscene_preview(self)

    def execute(self, context):
        if os.path.isdir(self.filepath):
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}

        lowered = self.filepath.lower()
        if not lowered.endswith(".w2cutscene"):
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}

        if not self.cutscene_actor_items and not self.cutscene_animation_items:
            _update_cutscene_preview(self)

        selected_actor_indices = {
            item.source_index
            for item in self.cutscene_actor_items
            if item.selected
        }
        selected_animation_indices = {
            item.source_index
            for item in self.cutscene_animation_items
            if item.selected
        }

        if not selected_actor_indices and not selected_animation_indices:
            self.report({'WARNING'}, "Nothing selected to import.")
            return {'CANCELLED'}

        try:
            cutscene_data = import_cutscene.import_w3_cutscene(
                self.filepath,
                selected_actor_indices=selected_actor_indices if self.cutscene_actor_items else None,
                selected_animation_indices=selected_animation_indices if self.cutscene_animation_items else None,
                auto_apply_selected_animations=self.auto_apply_animations,
            )
        except Exception as exc:
            log.exception("Failed to import cutscene %s", self.filepath)
            self.report({'ERROR'}, f"Failed to import cutscene: {exc}")
            return {'CANCELLED'}
        if cutscene_data is None:
            self.report({'ERROR'}, "Failed to load cutscene file.")
            return {'CANCELLED'}

        auto_loaded_count = int(getattr(cutscene_data, "auto_applied_animation_count", 0) or 0)
        import_duration_seconds = float(getattr(cutscene_data, "import_duration_seconds", 0.0) or 0.0)
        _sync_loaded_cutscene_state(context.scene, self.filepath, cutscene_data=cutscene_data)
        self.report(
            {'INFO'},
            (
                f"Imported {len(selected_actor_indices)} actor(s) and auto-loaded "
                f"{auto_loaded_count}/{len(selected_animation_indices)} animation(s) in {import_duration_seconds:.2f}s."
                if self.auto_apply_animations
                else (
                    f"Imported {len(selected_actor_indices)} actor(s) and listed "
                    f"{len(selected_animation_indices)} animation(s) from cutscene in {import_duration_seconds:.2f}s."
                )
            ),
        )
        return {'FINISHED'}
    def invoke(self, context, event):
        UNCOOK_PATH = os.path.join(get_uncook_path(context),"animations\\")
        if os.path.exists(UNCOOK_PATH):
            self.filepath = UNCOOK_PATH if self.filepath == '' else self.filepath
        return ImportHelper.invoke(self, context, event)

class WITCH_OT_ReopenCutsceneImportDialog(bpy.types.Operator):
    bl_idname = "witcher.reopen_cutscene_import_dialog"
    bl_label = "Open Import Dialog"
    bl_description = "Open the cutscene import dialog for the current cutscene"

    def execute(self, context):
        filepath = str(getattr(context.scene, "witcher_loaded_w2cutscene_path", "") or "").strip()
        if filepath:
            bpy.ops.witcher.import_w2_cutscene('INVOKE_DEFAULT', filepath=filepath)
        else:
            bpy.ops.witcher.import_w2_cutscene('INVOKE_DEFAULT')
        return {'FINISHED'}

class WITCH_OT_SetCutsceneActorLoaded(bpy.types.Operator):
    bl_idname = "witcher.set_cutscene_actor_loaded"
    bl_label = "Toggle Cutscene Actor"
    bl_description = "Load or unload a cutscene actor"
    bl_options = {'REGISTER', 'UNDO'}

    source_index: IntProperty(default=-1)
    load: BoolProperty(default=True)

    def execute(self, context):
        scene = context.scene
        actor_entry = _find_loaded_cutscene_actor_entry(scene, self.source_index)
        if actor_entry is None:
            self.report({'ERROR'}, "Cutscene actor entry not found.")
            return {'CANCELLED'}
        actor_state = _get_cutscene_actor_display_state(actor_entry)

        if self.load:
            actor_obj = _load_cutscene_actor_entry(scene, actor_entry)
            if actor_obj is None:
                self.report({'ERROR'}, "Failed to load cutscene actor.")
                return {'CANCELLED'}
            if any(
                bool(animation_entry.is_loaded)
                for animation_entry in scene.witcher_cutscene_animation_items
                if _animation_matches_actor_entry(scene, animation_entry, actor_entry)
            ):
                _rebuild_cutscene_actor_animations(scene, actor_entry)
            self.report({'INFO'}, f"Loaded actor '{actor_entry.label or actor_entry.actor_name or actor_obj.name}'.")
            return {'FINISHED'}

        actor_obj = actor_state["actor_obj"]
        import_cutscene.unload_cutscene_actor(actor_obj)
        actor_entry.object_name = ""
        actor_entry.cutscene_guid = ""
        actor_entry.is_loaded = False
        actor_entry.imported_by_cutscene = False
        for animation_entry in scene.witcher_cutscene_animation_items:
            if _animation_matches_actor_entry(scene, animation_entry, actor_entry):
                animation_entry.is_loaded = False
        self.report({'INFO'}, f"Unloaded actor '{actor_entry.label or actor_entry.actor_name or self.source_index}'.")
        return {'FINISHED'}

class WITCH_OT_SetCutsceneAnimationLoaded(bpy.types.Operator):
    bl_idname = "witcher.set_cutscene_animation_loaded"
    bl_label = "Toggle Cutscene Animation"
    bl_description = "Load or unload a cutscene animation"
    bl_options = {'REGISTER', 'UNDO'}

    source_index: IntProperty(default=-1)
    load: BoolProperty(default=True)

    def execute(self, context):
        scene = context.scene
        animation_entry = _find_loaded_cutscene_animation_entry(scene, self.source_index)
        if animation_entry is None:
            self.report({'ERROR'}, "Cutscene animation entry not found.")
            return {'CANCELLED'}

        actor_entry = _find_actor_entry_for_animation(scene, animation_entry)
        if actor_entry is None:
            self.report({'ERROR'}, "No matching cutscene actor found for this animation.")
            return {'CANCELLED'}
        actor_state = _get_cutscene_actor_display_state(actor_entry)

        if self.load and not actor_state["is_loaded"]:
            actor_obj = _load_cutscene_actor_entry(scene, actor_entry)
            if actor_obj is None:
                self.report({'ERROR'}, "Failed to load the actor required by this animation.")
                return {'CANCELLED'}

        animation_entry.is_loaded = bool(self.load)
        applied_indices, error_messages = _rebuild_cutscene_actor_animations(scene, actor_entry)
        if self.load and int(animation_entry.source_index) not in applied_indices:
            animation_entry.is_loaded = False
            error_text = str(error_messages.get(int(animation_entry.source_index), "") or "").strip()
            message = f"Failed to load cutscene animation '{_get_cutscene_animation_label(animation_entry)}'"
            if error_text:
                message = f"{message}: {error_text}"
            self.report({'ERROR'}, message)
            return {'CANCELLED'}

        if self.load:
            self.report({'INFO'}, f"Loaded animation '{_get_cutscene_animation_label(animation_entry)}'.")
        else:
            self.report({'INFO'}, f"Unloaded animation '{_get_cutscene_animation_label(animation_entry)}'.")
        return {'FINISHED'}

from ..ui.ui_utils import WITCH_PT_Base


def _draw_event_list_item(self, layout, item):
    if self.layout_type in {'DEFAULT', 'COMPACT'}:
        row = layout.row(align=True)
        row.label(text=_get_cutscene_event_label(item), icon=_event_type_icon(item.event_type))
        row.label(text=f"{item.start_time:.2f}s")
        if item.duration > 0.0:
            row.label(text=f"[{item.duration:.2f}s]")
    elif self.layout_type == 'GRID':
        layout.alignment = 'CENTER'
        layout.label(text="")


class WITCH_UL_RootEventList(UIList):
    bl_idname = "WITCH_UL_RootEventList"
    layout_type = "DEFAULT"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index, flt_flag):
        _draw_event_list_item(self, layout, item)

    def filter_items(self, context, data, propname):
        items = getattr(data, propname, [])
        flags = [self.bitflag_filter_item if str(getattr(i, "event_scope", "") or "").upper() == "ROOT" else 0
                 for i in items]
        return flags, []


class WITCH_UL_EntryEventList(UIList):
    bl_idname = "WITCH_UL_EntryEventList"
    layout_type = "DEFAULT"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index, flt_flag):
        _draw_event_list_item(self, layout, item)

    def filter_items(self, context, data, propname):
        items = getattr(data, propname, [])
        scene = context.scene
        anims = list(getattr(scene, "witcher_cutscene_animation_items", []))
        anim_ui_idx = getattr(scene, "witcher_cutscene_loaded_anim_index", 0)
        anim_src_idx = -1
        if 0 <= anim_ui_idx < len(anims):
            anim_src_idx = int(getattr(anims[anim_ui_idx], "source_index", -1))
        flags = [self.bitflag_filter_item
                 if (str(getattr(i, "event_scope", "") or "").upper() == "ENTRY"
                     and int(getattr(i, "source_index", -1)) == anim_src_idx)
                 else 0
                 for i in items]
        return flags, []


def _draw_event_detail(layout, ev):
    detail_box = layout.box()
    detail_row = detail_box.row(align=True)
    detail_row.label(text=_get_cutscene_event_label(ev), icon=_event_type_icon(ev.event_type))
    col = detail_box.column(align=True)
    col.use_property_split = True
    col.enabled = False
    col.prop(ev, "event_name")
    col.prop(ev, "start_time")
    col.prop(ev, "duration")
    col.prop(ev, "track_name")
    if ev.animation_name:
        col.prop(ev, "animation_name")
    if ev.effect_name:
        col.prop(ev, "effect_name")
    if ev.appearance or 'BodyPart' in ev.event_type or 'Appearance' in ev.event_type:
        col.prop(ev, "appearance")


def _draw_imported_class_sections(layout, field_items, schema, show_unset, empty_label):
    visible_any = False
    for class_name, _fields in schema:
        class_items = [
            item for item in field_items
            if str(getattr(item, "class_name", "") or "") == class_name
            and (show_unset or bool(getattr(item, "is_set", False)))
        ]
        if not class_items:
            continue

        visible_any = True
        class_box = layout.box()
        class_box.label(text=class_name, icon='PROPERTIES')
        col = class_box.column(align=True)
        col.use_property_split = True
        col.enabled = False
        for item in class_items:
            col.prop(item, "value_text", text=item.field_name)

    if not visible_any:
        layout.label(text=empty_label, icon='INFO')


def _draw_cutscene_template_tab(layout, scene):
    layout.use_property_split = False
    layout.use_property_decorate = False

    path_row = layout.row()
    path_row.label(text=str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or ""), icon='FILE')

    template_box = layout.box()
    header = template_box.row(align=True)
    header.label(text="Imported Classes", icon='PROPERTIES')
    header.prop(scene, "witcher_cutscene_show_unset_template_fields", text="Show Unset", toggle=True)

    _draw_imported_class_sections(
        template_box,
        list(getattr(scene, "witcher_cutscene_template_fields", [])),
        w3_types.CUTSCENE_CLASS_FIELD_SCHEMA,
        bool(getattr(scene, "witcher_cutscene_show_unset_template_fields", False)),
        "No set imported values.",
    )


def _draw_cutscene_actors_tab(layout, scene):
    actors = list(getattr(scene, "witcher_cutscene_actor_items", []))
    loaded_count = sum(1 for a in actors if _get_cutscene_actor_display_state(a)["is_loaded"])

    if actors:
        layout.template_list(
            "WITCH_UL_LoadedActorList", "",
            scene, "witcher_cutscene_actor_items",
            scene, "witcher_cutscene_loaded_actor_index",
            rows=min(len(actors), 6),
        )
        idx = getattr(scene, "witcher_cutscene_loaded_actor_index", 0)
        if 0 <= idx < len(actors):
            actor = actors[idx]
            state = _get_cutscene_actor_display_state(actor)
            detail_box = layout.box()
            detail_row = detail_box.row(align=True)
            label = actor.label or actor.actor_name or f"Actor {actor.source_index + 1}"
            detail_row.label(text=label, icon='ARMATURE_DATA')
            if state["is_loaded"]:
                op = detail_row.operator(WITCH_OT_SetCutsceneActorLoaded.bl_idname, text="Unload", icon='X')
                op.source_index = actor.source_index
                op.load = False
            else:
                op = detail_row.operator(WITCH_OT_SetCutsceneActorLoaded.bl_idname, text="Load", icon='IMPORT')
                op.source_index = actor.source_index
                op.load = True
            col = detail_box.column(align=True)
            if actor.voice_tag:
                col.label(text=f"Voice Tag:  {actor.voice_tag}")
            if actor.template_path:
                col.label(text=actor.template_path, icon='FILE_3D')
            if actor.appearance_name:
                col.label(text=f"Appearance:  {actor.appearance_name}", icon='MATERIAL_DATA')
            if actor.actor_type:
                col.label(text=f"Type:  {actor.actor_type.replace('CAT_', '')}")
            if actor.use_mimic:
                col.label(text="Uses mimic data", icon='FACE_MAPS')
            if state["is_loaded"] and not state["imported_by_cutscene"]:
                col.label(text="Existing scene object", icon='LINKED')
    else:
        layout.label(text="No actors in cutscene.", icon='INFO')

    layout.label(text=f"Loaded: {loaded_count}/{len(actors)}")


def _draw_cutscene_anims_tab(layout, scene):
    anims = list(getattr(scene, "witcher_cutscene_animation_items", []))
    idx = getattr(scene, "witcher_cutscene_loaded_anim_index", 0)
    cs_selected = len(anims) > 0 and 0 <= idx < len(anims) and anims[idx].source_index == -1
    real_anims = [a for a in anims if a.source_index != -1]
    loaded_count = sum(1 for a in real_anims if _get_cutscene_animation_display_state(scene, a)["is_loaded"])

    if anims:
        layout.template_list(
            "WITCH_UL_LoadedAnimList", "",
            scene, "witcher_cutscene_animation_items",
            scene, "witcher_cutscene_loaded_anim_index",
            rows=min(len(anims), 8),
        )
        # Only show detail / load-unload when an animation (not Cutscene sentinel) is active
        if not cs_selected:
            if 0 <= idx < len(anims):
                anim = anims[idx]
                anim_state = _get_cutscene_animation_display_state(scene, anim)
                detail_box = layout.box()
                detail_row = detail_box.row(align=True)
                detail_row.label(text=_get_cutscene_animation_label(anim), icon='ACTION')
                if anim_state["is_loaded"]:
                    op = detail_row.operator(WITCH_OT_SetCutsceneAnimationLoaded.bl_idname, text="Unload", icon='X')
                    op.source_index = anim.source_index
                    op.load = False
                else:
                    op = detail_row.operator(WITCH_OT_SetCutsceneAnimationLoaded.bl_idname, text="Load", icon='IMPORT')
                    op.source_index = anim.source_index
                    op.load = True
                col = detail_box.column(align=True)
                if anim.component_name:
                    col.label(text=f"Component: {anim.component_name}", icon='BONE_DATA')
                if anim.frames_per_second:
                    col.label(text=f"FPS: {anim.frames_per_second:.1f}   Frames: {anim.num_frames}")
                if anim.duration:
                    col.label(text=f"Duration: {anim.duration:.3f}s")
    else:
        layout.label(text="No animations in cutscene.", icon='INFO')

    layout.label(text=f"Loaded: {loaded_count}/{len(real_anims)}")


def _draw_cutscene_events_tab(layout, scene):
    all_events = list(getattr(scene, "witcher_cutscene_event_items", []))
    anims = list(getattr(scene, "witcher_cutscene_animation_items", []))
    anim_ui_idx = getattr(scene, "witcher_cutscene_loaded_anim_index", 0)
    cs_selected = len(anims) == 0 or not (0 <= anim_ui_idx < len(anims)) or anims[anim_ui_idx].source_index == -1

    if cs_selected:
        root_events = [e for e in all_events if str(getattr(e, "event_scope", "") or "").upper() == "ROOT"]
        layout.label(text=f"Cutscene Events ({len(root_events)})", icon='SCENE_DATA')
        if root_events:
            layout.template_list(
                "WITCH_UL_RootEventList", "",
                scene, "witcher_cutscene_event_items",
                scene, "witcher_cutscene_event_index",
                rows=min(len(root_events), 6),
            )
            ev_idx = getattr(scene, "witcher_cutscene_event_index", 0)
            if 0 <= ev_idx < len(all_events):
                ev = all_events[ev_idx]
                if str(getattr(ev, "event_scope", "") or "").upper() == "ROOT":
                    _draw_event_detail(layout, ev)
        else:
            layout.label(text="No cutscene events.", icon='INFO')

    else:
        if anims and 0 <= anim_ui_idx < len(anims):
            anim = anims[anim_ui_idx]
            anim_src_idx = int(getattr(anim, "source_index", -1))
            anim_label = _get_cutscene_animation_label(anim)
            entry_events = [e for e in all_events
                            if str(getattr(e, "event_scope", "") or "").upper() == "ENTRY"
                            and int(getattr(e, "source_index", -1)) == anim_src_idx]
            layout.label(text=f"{anim_label} ({len(entry_events)})", icon='ACTION')
            if entry_events:
                layout.template_list(
                    "WITCH_UL_EntryEventList", "",
                    scene, "witcher_cutscene_event_items",
                    scene, "witcher_cs_entry_event_idx",
                    rows=min(len(entry_events), 6),
                )
                ev_idx = getattr(scene, "witcher_cs_entry_event_idx", 0)
                if 0 <= ev_idx < len(all_events):
                    ev = all_events[ev_idx]
                    if str(getattr(ev, "event_scope", "") or "").upper() == "ENTRY":
                        _draw_event_detail(layout, ev)
            else:
                layout.label(text="No events for this animation.", icon='INFO')
        else:
            layout.label(text="Select an animation in the Animations tab.", icon='INFO')

    layout.separator()
    dialog_header = layout.row(align=True)
    dialog_header.label(text="Dialogs", icon='OUTLINER_OB_SPEAKER')
    dialog_header.operator(WITCH_OT_LoadCutsceneDialogs.bl_idname, text="Load", icon='FILE_REFRESH')

    dialog_items = list(getattr(scene, "witcher_cutscene_dialog_items", []))
    if dialog_items:
        layout.template_list(
            "WITCH_UL_CutsceneDialogList", "",
            scene, "witcher_cutscene_dialog_items",
            scene, "witcher_cutscene_dialog_index",
            rows=min(len(dialog_items), 4),
        )
        sel_idx = getattr(scene, "witcher_cutscene_dialog_index", 0)
        if 0 <= sel_idx < len(dialog_items):
            sel = dialog_items[sel_idx]
            detail = layout.box()
            detail.label(text=sel.actor or "?", icon='OUTLINER_OB_SPEAKER')
            col = detail.column(align=True)
            if sel.scene_path:
                col.label(text=f"Scene: {sel.scene_path}", icon='FILE')
            col.label(text=f"Voice: {sel.voice_file or '-'}")
            col.label(text=f"Sound: {sel.sound_event or '-'}")
            col.label(text=f"Line: {sel.line_index}")
    else:
        layout.label(text="Press 'Load' to fetch dialog lines from linked .w2scene.", icon='INFO')


class WITCHER_PT_scene_panel(WITCH_PT_Base, Panel):
    #bl_parent_id = "WITCH_PT_ENTITY_Panel"
    bl_idname = "WITCHER_PT_scene_panel"
    bl_label = "Scene / Cutscene"
    bl_description = ""
    #bl_options = {'HEADER_LAYOUT_EXPAND'}
    bl_options = {'DEFAULT_CLOSED'}

    def draw_header(self, context):
        self.layout.label(text="", icon='SCENE_DATA')

    def draw(self, context):
        scene = context.scene
        if scene is None:
            return

        cs_box = self.layout.box()
        cs_box.label(text="Cutscene (.w2cutscene)", icon='SCENE_DATA')
        cs_box.operator(ButtonOperatorImportW2cutscene.bl_idname, text="Import CS (.w2cutscene)", icon='IMPORT')

        loaded_cutscene_path = str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or "").strip()
        if loaded_cutscene_path and not scene.witcher_cutscene_actor_items and not scene.witcher_cutscene_animation_items:
            _schedule_deferred_cutscene_state_sync(scene, loaded_cutscene_path)

        if loaded_cutscene_path:
            header = cs_box.row(align=True)
            cs_name = scene.witcher_loaded_cutscene_name or _get_loaded_cutscene_name(loaded_cutscene_path)
            header.label(text=cs_name, icon='ACTION')
            header.operator(WITCH_OT_ReopenCutsceneImportDialog.bl_idname, text="", icon='FILE_REFRESH')
            last_import_seconds = float(getattr(scene, "witcher_cutscene_last_import_seconds", 0.0) or 0.0)
            if last_import_seconds > 0.0:
                cs_box.label(text=f"Last import: {last_import_seconds:.2f}s", icon='TIME')

            prev_split = cs_box.use_property_split
            cs_box.use_property_split = False
            tab_row = cs_box.row(align=True)
            tab_row.prop_enum(scene, "witcher_cs_tab", 'TEMPLATE')
            tab_row.prop_enum(scene, "witcher_cs_tab", 'ACTORS')
            tab_row.prop_enum(scene, "witcher_cs_tab", 'ANIMS')
            tab_row.prop_enum(scene, "witcher_cs_tab", 'EVENTS')
            cs_box.use_property_split = prev_split
            cs_box.separator(factor=0.5)

            tab = str(getattr(scene, "witcher_cs_tab", "ACTORS") or "ACTORS")
            if tab == 'TEMPLATE':
                _draw_cutscene_template_tab(cs_box, scene)
            elif tab == 'ACTORS':
                _draw_cutscene_actors_tab(cs_box, scene)
            elif tab == 'ANIMS':
                _draw_cutscene_anims_tab(cs_box, scene)
            elif tab == 'EVENTS':
                _draw_cutscene_events_tab(cs_box, scene)

        self.layout.separator()
        w2s_box = self.layout.box()
        w2s_box.label(text="Scene (.w2scene)", icon='WORLD')
        w2s_box.operator(ButtonOperatorImportW2scene.bl_idname, text="Import Scene (.w2scene)", icon='IMPORT')

        row = w2s_box.row()
        col = row.column(align=True)
        col.template_list("WITCHER_SECTIONS_UL_List", "", scene, "witcher_sections", scene, "witcher_sections_index")
        col = row.column()
        w2s_box.operator(Witcher_OT_load_section.bl_idname, text="Load Section")

class WITCHER_SECTIONS_UL_List(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        # Draw the scene section name
        layout.label(text=item.name)

class Witcher_OT_load_section(bpy.types.Operator):
    bl_idname = "witcher.load_section"
    bl_label = "Print Selected Index"
    bl_description = "Print the index of the selected scene section"

    def execute(self, context):
        # Get the scene object
        scene = context.scene

        # Print the index of the selected scene section
        log.debug("Selected Index: %s", scene.witcher_sections_index)

        sceneImporter = import_scene.import_w3_scene(context.scene.witcher_sections_filepath)
        sceneImporter.load_sections()
        this_section = sceneImporter.scene_sections[scene.witcher_sections_index]
        log.debug("Section: %s", this_section.sectionName)
        sceneImporter.load_section(this_section)
        sceneImporter.execute()


        return {'FINISHED'}

class WITCHER_PT_witcher_sections_panel(WITCH_PT_Base, Panel):
    bl_parent_id = "WITCHER_PT_scene_panel"
    bl_idname = "WITCHER_PT_witcher_sections_panel"
    bl_label = "Scene Sections"
    bl_description = ""
    #bl_options = {'HEADER_LAYOUT_EXPAND'}
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        object = context.scene
        if object == None:
            return

        box = self.layout.box()
        row = box.row()
        col = row.column(align=True)
        col.template_list("WITCHER_SECTIONS_UL_List", "", object, "witcher_sections", object, "witcher_sections_index")
        col = row.column()
        box.operator(Witcher_OT_load_section.bl_idname, text="Load Section")

classes = [
    WitcherSection,
    CutsceneActorPreviewItem,
    CutsceneAnimationPreviewItem,
    CutsceneLoadedActorItem,
    CutsceneLoadedAnimationItem,
    CutsceneEventItem,
    CutsceneEffectItem,
    CutsceneTemplateFieldItem,
    CutsceneDialogItem,
    WITCH_UL_CutsceneActorPreview,
    WITCH_UL_CutsceneAnimationPreview,
    WITCH_UL_CutsceneDialogList,
    WITCH_UL_LoadedActorList,
    WITCH_UL_LoadedAnimList,
    WITCH_UL_RootEventList,
    WITCH_UL_EntryEventList,
    ButtonOperatorImportW2cutscene,
    WITCH_OT_ReopenCutsceneImportDialog,
    WITCH_OT_SetCutsceneActorLoaded,
    WITCH_OT_SetCutsceneAnimationLoaded,
    WITCH_OT_LoadCutsceneDialogs,
    ButtonOperatorImportW2scene,
    WITCHER_PT_scene_panel,
    WITCHER_SECTIONS_UL_List,
    Witcher_OT_load_section,
    #WITCHER_PT_witcher_sections_panel,
]




def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.witcher_sections = bpy.props.CollectionProperty(type=WitcherSection)
    bpy.types.Scene.witcher_sections_index = bpy.props.IntProperty(default=0)
    bpy.types.Scene.witcher_sections_filepath = bpy.props.StringProperty(default="")
    bpy.types.Scene.witcher_loaded_cutscene_name = bpy.props.StringProperty(default="")
    bpy.types.Scene.witcher_cutscene_last_import_seconds = bpy.props.FloatProperty(default=0.0)
    bpy.types.Scene.witcher_cutscene_actor_items = bpy.props.CollectionProperty(type=CutsceneLoadedActorItem)
    bpy.types.Scene.witcher_cutscene_animation_items = bpy.props.CollectionProperty(type=CutsceneLoadedAnimationItem)
    bpy.types.Scene.witcher_cutscene_event_items = bpy.props.CollectionProperty(type=CutsceneEventItem)
    bpy.types.Scene.witcher_cutscene_event_index = bpy.props.IntProperty(default=0)
    bpy.types.Scene.witcher_cs_entry_event_idx = bpy.props.IntProperty(default=0)
    bpy.types.Scene.witcher_cs_tab = bpy.props.EnumProperty(
        name="Cutscene Tab",
        items=[
            ('TEMPLATE', 'Template', 'Cutscene template properties and linked scenes'),
            ('ACTORS', 'Actors', 'Manage cutscene actors'),
            ('ANIMS', 'Animations', 'Manage cutscene animations'),
            ('EVENTS', 'Events', 'Cutscene events and dialog lines'),
        ],
        default='ACTORS',
    )
    bpy.types.Scene.witcher_cutscene_loaded_actor_index = bpy.props.IntProperty(default=0)
    bpy.types.Scene.witcher_cutscene_loaded_anim_index = bpy.props.IntProperty(default=0)
    bpy.types.Scene.witcher_cutscene_template_fields = bpy.props.CollectionProperty(type=CutsceneTemplateFieldItem)
    bpy.types.Scene.witcher_cutscene_show_unset_template_fields = bpy.props.BoolProperty(name="Show Unset", default=False)
    bpy.types.Scene.witcher_cutscene_effect_items = bpy.props.CollectionProperty(type=CutsceneEffectItem)
    bpy.types.Scene.witcher_cutscene_dialog_items = bpy.props.CollectionProperty(type=CutsceneDialogItem)
    bpy.types.Scene.witcher_cutscene_dialog_index = bpy.props.IntProperty(default=0)

def unregister():
    if hasattr(bpy.types.Scene, "witcher_sections"):
        del bpy.types.Scene.witcher_sections
    if hasattr(bpy.types.Scene, "witcher_sections_index"):
        del bpy.types.Scene.witcher_sections_index
    if hasattr(bpy.types.Scene, "witcher_sections_filepath"):
        del bpy.types.Scene.witcher_sections_filepath
    if hasattr(bpy.types.Scene, "witcher_loaded_cutscene_name"):
        del bpy.types.Scene.witcher_loaded_cutscene_name
    if hasattr(bpy.types.Scene, "witcher_cutscene_last_import_seconds"):
        del bpy.types.Scene.witcher_cutscene_last_import_seconds
    if hasattr(bpy.types.Scene, "witcher_cutscene_actor_items"):
        del bpy.types.Scene.witcher_cutscene_actor_items
    if hasattr(bpy.types.Scene, "witcher_cutscene_animation_items"):
        del bpy.types.Scene.witcher_cutscene_animation_items
    if hasattr(bpy.types.Scene, "witcher_cutscene_event_items"):
        del bpy.types.Scene.witcher_cutscene_event_items
    if hasattr(bpy.types.Scene, "witcher_cutscene_event_index"):
        del bpy.types.Scene.witcher_cutscene_event_index
    for prop in ("witcher_cs_tab", "witcher_cutscene_loaded_actor_index", "witcher_cutscene_loaded_anim_index",
                 "witcher_cutscene_show_unset_template_fields",
                 "witcher_cs_entry_event_idx",
                 # legacy props removed in this version:
                 "witcher_cs_fade_before", "witcher_cs_fade_after", "witcher_cs_cam_blend_in", "witcher_cs_cam_blend_out",
                 "witcher_cs_blackscreen", "witcher_cs_check_actors_pos", "witcher_cs_reverb_name",
                 "witcher_cs_audio_track", "witcher_cs_ent_to_hide_tags",
                 "witcher_cutscene_info_tab", "witcher_cutscene_event_scope_tab", "witcher_cutscene_events_tab",
                 "witcher_cs_events_anim_idx", "witcher_cs_event_view"):
        if hasattr(bpy.types.Scene, prop):
            delattr(bpy.types.Scene, prop)
    if hasattr(bpy.types.Scene, "witcher_cutscene_template_fields"):
        del bpy.types.Scene.witcher_cutscene_template_fields
    if hasattr(bpy.types.Scene, "witcher_cutscene_effect_items"):
        del bpy.types.Scene.witcher_cutscene_effect_items
    if hasattr(bpy.types.Scene, "witcher_cutscene_dialog_items"):
        del bpy.types.Scene.witcher_cutscene_dialog_items
    if hasattr(bpy.types.Scene, "witcher_cutscene_dialog_index"):
        del bpy.types.Scene.witcher_cutscene_dialog_index
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
