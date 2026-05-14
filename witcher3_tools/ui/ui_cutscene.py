import logging
import math
import os

import bpy
import blf
from bpy.types import Panel, Operator, UIList, PropertyGroup
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    StringProperty,
)
from bpy_extras.io_utils import ImportHelper

from .. import dialog_language
from .. import get_uncook_path
from ..CR2W import w3_types
from ..importers import import_cutscene
from ..exporters import export_cutscene
from ..camera_tracks import (
    CAMERA_TRACK_NAMES,
    ensure_camera_track_properties,
)
from .ui_cr2w_fields import (
    _draw_imported_class_sections,
    _format_imported_field_value,
    _get_imported_field_schema,
    _get_imported_field_value,
    _get_present_imported_fields,
)
from ..ui.ui_utils import WITCH_PT_Base

log = logging.getLogger(__name__)

_CUTSCENE_SYNC_DEFERRED = set()
_SCRATCH_CUTSCENE_TRACK_NAME = "cutscene_anim"
_CUTSCENE_IMPORT_NLA_TRACK_COMPONENTS = {
    "anim_import": "Root",
    "mimic_import": "face",
}
_subtitle_draw_handle = None


def _scene_fps(scene):
    render = getattr(scene, "render", None)
    fps = float(getattr(render, "fps", 30.0) or 30.0)
    fps_base = float(getattr(render, "fps_base", 1.0) or 1.0)
    return fps / fps_base if fps_base else fps


def _strip_get(strip, prop_name, default=""):
    try:
        return strip.get(prop_name, default)
    except Exception:
        return default


def _subtitle_text_from_strip(strip):
    for prop_name in (
        dialog_language.DIALOG_SUBTITLE_TEXT_PROP,
        import_cutscene.CUTSCENE_DIALOG_TEXT_PROP,
        "witcher_w2scene_dialog_text",
    ):
        text = str(_strip_get(strip, prop_name, "") or "").strip()
        if text:
            return text
    return ""


def _active_subtitle_from_sound_strips(scene, frame):
    try:
        from .ui_voice import _get_sequence_editor_strips
    except Exception:
        return None

    strips = _get_sequence_editor_strips(getattr(scene, "sequence_editor", None))
    if strips is None:
        return None

    frame = float(frame)
    for strip in list(strips):
        if getattr(strip, "type", None) != 'SOUND':
            continue
        text = _subtitle_text_from_strip(strip)
        if not text:
            continue
        start = float(getattr(strip, "frame_final_start", getattr(strip, "frame_start", 0.0)) or 0.0)
        end = float(getattr(strip, "frame_final_end", start) or start)
        if end <= start:
            end = start + 1.0
        if start <= frame <= end:
            return text
    return None


def _active_subtitle_from_cutscene_items(scene, frame):
    for item in getattr(scene, "witcher_cutscene_dialog_items", []) or []:
        text = str(getattr(item, "line_text", "") or "").strip()
        if not text:
            continue
        try:
            start_frame = int(getattr(item, "start_frame", 0) or 0)
            end_frame = int(getattr(item, "end_frame", 0) or 0)
        except Exception:
            continue
        if start_frame <= int(frame) <= end_frame:
            return text
    return None


def _active_subtitle_from_w2scene_elements(scene, frame):
    try:
        active_section_index = int(getattr(scene, "witcher_w2scene_active_subtitle_section_index", -1))
    except Exception:
        active_section_index = -1
    if active_section_index < 0:
        return None
    fps = _scene_fps(scene)
    frame_offset = float(getattr(scene, "witcher_w2scene_section_subtitle_frame_offset", 0.0) or 0.0)
    frame = float(frame)
    for item in getattr(scene, "witcher_w2scene_section_element_items", []) or []:
        try:
            if int(getattr(item, "section_index", -1)) != active_section_index:
                continue
        except Exception:
            continue
        if str(getattr(item, "element_type", "") or "") != "CStorySceneLine":
            continue
        text = str(getattr(item, "line_text", "") or "").strip()
        if not text:
            continue
        try:
            start_frame = frame_offset + (float(getattr(item, "start_time", 0.0) or 0.0) * fps)
            duration_frames = max(1.0, float(getattr(item, "duration", 0.0) or 0.0) * fps)
        except Exception:
            continue
        if start_frame <= frame <= (start_frame + duration_frames):
            return text
    return None


def _cutscene_get_active_subtitle(scene, frame):
    if scene is None or not bool(getattr(scene, "witcher_cutscene_show_dialog_subtitles", True)):
        return None
    for getter in (
        _active_subtitle_from_cutscene_items,
        _active_subtitle_from_sound_strips,
        _active_subtitle_from_w2scene_elements,
    ):
        text = getter(scene, frame)
        if text:
            return text
    return None


def _set_blf_size(font_id, font_size):
    try:
        blf.size(font_id, font_size)
    except TypeError:
        blf.size(font_id, font_size, 72)


def _wrap_subtitle_text(font_id, text, max_width):
    words = str(text or "").split()
    if not words:
        return []

    lines = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        width, _height = blf.dimensions(font_id, candidate)
        if current and width > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def draw_cutscene_subtitle():
    context = bpy.context
    scene = getattr(context, "scene", None)
    region = getattr(context, "region", None)
    if scene is None or region is None:
        return

    text = _cutscene_get_active_subtitle(scene, getattr(scene, "frame_current", 0))
    if not text:
        return

    font_id = 0
    font_size = int(getattr(scene, "witcher_cutscene_subtitle_font_size", 28) or 28)
    font_size = max(12, min(72, font_size))
    _set_blf_size(font_id, font_size)

    max_width = max(120.0, float(getattr(region, "width", 0) or 0) * 0.82)
    lines = _wrap_subtitle_text(font_id, text, max_width) or [text]
    line_height = font_size * 1.25
    base_y = 60.0

    for idx, line in enumerate(reversed(lines)):
        text_width, _text_height = blf.dimensions(font_id, line)
        x = (float(region.width) - text_width) / 2.0
        y = base_y + (idx * line_height)

        try:
            blf.color(font_id, 0.0, 0.0, 0.0, 0.85)
        except Exception:
            pass
        blf.position(font_id, x + 2.0, y - 2.0, 0)
        blf.draw(font_id, line)

        try:
            blf.color(font_id, 1.0, 1.0, 1.0, 1.0)
        except Exception:
            pass
        blf.position(font_id, x, y, 0)
        blf.draw(font_id, line)


def enable_cutscene_subtitles():
    global _subtitle_draw_handle
    if _subtitle_draw_handle is not None:
        return
    try:
        _subtitle_draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            draw_cutscene_subtitle,
            (),
            'WINDOW',
            'POST_PIXEL',
        )
    except Exception:
        _subtitle_draw_handle = None
        log.debug("Could not register cutscene subtitle draw handler.", exc_info=True)


def disable_cutscene_subtitles():
    global _subtitle_draw_handle
    if _subtitle_draw_handle is None:
        return
    try:
        bpy.types.SpaceView3D.draw_handler_remove(_subtitle_draw_handle, 'WINDOW')
    except Exception:
        log.debug("Could not remove cutscene subtitle draw handler.", exc_info=True)
    _subtitle_draw_handle = None


def _cs_text(value):
    return str(value or "").strip()


def _cs_yield_once(obj, seen):
    if obj is None or getattr(obj, "type", None) != 'ARMATURE':
        return
    obj_name = _cs_text(getattr(obj, "name", ""))
    if not obj_name or obj_name in seen:
        return
    seen.add(obj_name)
    yield obj


def _cs_mimic_face_armature(actor_obj):
    if actor_obj is None:
        return None
    mimic_name = _cs_text(actor_obj.get("mimicFace", ""))
    if mimic_name:
        mimic_obj = bpy.data.objects.get(mimic_name)
        if mimic_obj is not None and getattr(mimic_obj, "type", None) == 'ARMATURE':
            return mimic_obj
    return None


def _cs_iter_import_clip_armatures(actor_obj, scene):
    seen = set()
    try:
        related = export_cutscene._iter_cutscene_related_armatures(actor_obj, scene)
    except Exception:
        related = (actor_obj,)
    for obj in related:
        yield from _cs_yield_once(obj, seen)
    yield from _cs_yield_once(_cs_mimic_face_armature(actor_obj), seen)


def _collect_cutscene_import_nla_candidates(scene):
    candidates = []
    try:
        actor_roots = export_cutscene._collect_cutscene_actor_roots(scene)
    except Exception:
        actor_roots = []
    for actor_obj in actor_roots:
        actor_name = _cs_text(actor_obj.get("cutscene_actor_name", ""))
        if not actor_name:
            continue
        for source_obj in _cs_iter_import_clip_armatures(actor_obj, scene):
            anim_data = getattr(source_obj, "animation_data", None)
            if anim_data is None:
                continue
            for track in getattr(anim_data, "nla_tracks", []) or []:
                track_name = _cs_text(getattr(track, "name", ""))
                component = _CUTSCENE_IMPORT_NLA_TRACK_COMPONENTS.get(track_name)
                if component is None or getattr(track, "mute", False):
                    continue
                for strip in getattr(track, "strips", []) or []:
                    if getattr(strip, "mute", False):
                        continue
                    action = getattr(strip, "action", None)
                    if action is None:
                        continue
                    candidates.append({
                        "actor_name": actor_name,
                        "actor_object_name": getattr(actor_obj, "name", ""),
                        "source_object_name": getattr(source_obj, "name", ""),
                        "track_name": track_name,
                        "strip_name": _cs_text(getattr(strip, "name", "")),
                        "action_name": _cs_text(getattr(action, "name", "")),
                        "component": component,
                        "frame_start": float(getattr(strip, "frame_start", 0.0) or 0.0),
                        "frame_end": float(getattr(strip, "frame_end", 0.0) or 0.0),
                    })
    candidates.sort(key=lambda item: (
        item["actor_name"].lower(),
        item["frame_start"],
        item["component"].lower(),
        item["source_object_name"].lower(),
        item["strip_name"].lower(),
    ))
    return candidates


def _cs_find_camera_armature(context):
    scene = getattr(context, "scene", None)
    if scene is None:
        return None
    for obj in scene.objects:
        if getattr(obj, "type", None) == 'ARMATURE':
            actor_type = str(obj.get("cutscene_actor_type", "") or "")
            if actor_type == "CAT_Camera" or str(obj.get("cutscene_actor_name", "") or "").lower() == "camera":
                return obj
    return None

def _cs_iter_camera_cuts(armature_obj):
    if armature_obj is None or not getattr(armature_obj, "animation_data", None):
        return []
    cuts = []
    for track in (armature_obj.animation_data.nla_tracks or []):
        if str(getattr(track, "name", "") or "") != _SCRATCH_CUTSCENE_TRACK_NAME:
            continue
        for strip in (track.strips or []):
            cuts.append((track, strip))
    cuts.sort(key=lambda ts: ts[1].frame_start)
    return cuts


def _cs_current_cut_index(context, armature_obj, cuts):
    if not cuts:
        return -1
    scene = getattr(context, "scene", None)
    if scene is None:
        return -1
    frame = getattr(scene, "frame_current", 0)
    best = -1
    for i, (_, strip) in enumerate(cuts):
        if strip.frame_start <= frame:
            best = i
    return best

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
    tag: StringProperty(default="")
    voice_tag: StringProperty(default="")
    template_path: StringProperty(default="")
    appearance_name: StringProperty(default="")
    actor_type: StringProperty(default="")
    final_position: StringProperty(default="")
    kill_me: BoolProperty(default=False)
    use_mimic: BoolProperty(default=False)
    anim_final_pos: StringProperty(default="")
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
    always_fires_end: BoolProperty(name="Always Fires End", default=False)
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
    actor: StringProperty(name="voicetag", default="")
    voice_file: StringProperty(name="voiceFileName", default="")
    sound_event: StringProperty(name="soundEventName", default="")
    line_id: StringProperty(name="dialogLine", default="")
    line_index: IntProperty(name="dialogLine Int32", default=0)
    line_text: StringProperty(name="LocalizedString.text", default="")
    scene_path: StringProperty(name="source .w2scene", default="")
    start_frame: IntProperty(name="computed start frame", default=0)
    end_frame: IntProperty(name="computed end frame", default=0)
    imported_sound: BoolProperty(name="imported audio strip", default=False)

def _set_cutscene_burned_audio_scene_state(scene, event_name="", item_path=""):
    if hasattr(scene, "witcher_cutscene_burned_audio_event"):
        scene.witcher_cutscene_burned_audio_event = str(event_name or "")
    if hasattr(scene, "witcher_cutscene_burned_audio_item_path"):
        scene.witcher_cutscene_burned_audio_item_path = str(item_path or "")


def _join_cutscene_metadata_list(values):
    items = []
    for value in values or []:
        item_text = str(value or "").strip()
        if item_text:
            items.append(item_text)
    return "; ".join(items)


def _set_cutscene_export_metadata_scene_state(scene, point_tags=(), last_level_loaded="", used_in_files=(), synced=False):
    if hasattr(scene, export_cutscene.CUTSCENE_POINT_TAGS_PROP):
        scene.witcher_cutscene_point_tags = _join_cutscene_metadata_list(point_tags)
    if hasattr(scene, export_cutscene.CUTSCENE_LAST_LEVEL_LOADED_PROP):
        scene.witcher_cutscene_last_level_loaded = str(last_level_loaded or "")
    if hasattr(scene, export_cutscene.CUTSCENE_USED_IN_FILES_PROP):
        scene.witcher_cutscene_used_in_files = _join_cutscene_metadata_list(used_in_files)
    if hasattr(scene, export_cutscene.CUTSCENE_EXPORT_METADATA_SYNCED_PROP):
        scene.witcher_cutscene_export_metadata_synced = bool(synced)


def _sync_cutscene_export_metadata_state(scene, cutscene):
    if cutscene is None:
        _set_cutscene_export_metadata_scene_state(scene, synced=False)
        return

    point_tags = getattr(cutscene, "point", None) or []
    last_level_loaded = getattr(cutscene, "lastLevelLoaded", "")
    used_in_files = getattr(cutscene, "usedInFiles", None) or []
    _set_cutscene_export_metadata_scene_state(
        scene,
        point_tags=point_tags,
        last_level_loaded=last_level_loaded,
        used_in_files=used_in_files,
        synced=True,
    )

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


def _sync_cutscene_burned_audio_state(scene, filepath, cutscene, cutscene_data=None):
    event_name = ""
    item_path = ""
    if cutscene is not None:
        has_burned_audio_prop, burned_audio_name = import_cutscene.get_cutscene_burned_audio_property(cutscene)
        if has_burned_audio_prop:
            event_name = str(burned_audio_name or "").strip()

    burned_audio_info = dict(getattr(cutscene_data, "burned_audio_info", {}) or {})
    item_path = str(burned_audio_info.get("item_path", "") or "").strip()
    if event_name and not item_path:
        try:
            resolved_audio = import_cutscene.resolve_cutscene_burned_audio_item(cutscene, filename=filepath)
        except Exception:
            resolved_audio = None
        if resolved_audio:
            item_path = str(resolved_audio.get("item_path", "") or "").strip()

    _set_cutscene_burned_audio_scene_state(scene, event_name=event_name, item_path=item_path)

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
    if 'Dialog' in event_type or 'Lookat' in event_type or 'LookAt' in event_type:
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


# ── Event class schema ─────────────────────────────────────────────────────
# (class_name, base_class, scope, own_props)
# scope: "ROOT" = cutscene-level (CCutsceneTemplate.animevents)
#        "ENTRY" = animation-level (CSkeletalAnimationSetEntry.entries)
# own_props: fields added by this class beyond its base (name, type pairs)
_CUTSCENE_EVENT_SCHEMA = [
    # ── ROOT: cutscene-level events ─────────────────────────────────────────
    ("CExtAnimCutsceneFadeEvent",             "CExtAnimEvent",                 "ROOT",
        [("in","Bool"), ("duration","Float"), ("color","Color")]),
    ("CExtAnimCutsceneSlowMoEvent",           "CExtAnimCutsceneDurationEvent", "ROOT",
        [("enabled","Bool"), ("factor","Float"), ("useWeightCurve","Bool"), ("weightCurve","SCurveData")]),
    ("CExtAnimCutsceneWindEvent",             "CExtAnimCutsceneDurationEvent", "ROOT",
        [("enabled","Bool"), ("factor","Float"), ("useWeightCurve","Bool"), ("weightCurve","SCurveData")]),
    ("CExtAnimCutsceneEnvironmentEvent",      "CExtAnimEvent",                 "ROOT",
        [("environmentName","String"), ("environmentActivate","Bool"),
         ("stabilizeBlending","Bool"), ("instantEyeAdaptation","Bool"),
         ("instantDissolve","Bool"), ("forceNoOtherEnvironments","Bool"),
         ("forceSetupLocalEnvironments","Bool"), ("forceSetupGlobalEnvironments","Bool")]),
    ("CExtAnimCutsceneLightEvent",            "CExtAnimEvent",                 "ROOT",
        [("tag","TagList"), ("isEnabled","Bool"), ("radius","Float"), ("brightness","Float"), ("color","Color")]),
    ("CExtAnimCutsceneSoundEvent",            "CExtAnimEvent",                 "ROOT",
        [("soundEventName","StringAnsi"), ("bone","CName"), ("useMaterialInfo","Bool")]),
    ("CExtAnimCutsceneEffectEvent",           "CExtAnimDurationEvent",         "ROOT",
        [("effect","CName"), ("tag","TagList"), ("template","soft:CEntityTemplate"),
         ("spawnPosMS","Vector"), ("spawnRotMS","EulerAngles")]),
    ("CExtAnimCutsceneActorEffect",           "CExtAnimDurationEvent",         "ROOT",
        [("effectName","CName")]),
    ("CExtAnimCutsceneHideEntityEvent",       "CExtAnimCutsceneEvent",         "ROOT",
        [("entTohideTag","CName")]),
    ("CExtAnimCutsceneHideTerrainEvent",      "CExtAnimCutsceneDurationEvent", "ROOT", []),
    ("CExtAnimCutsceneSurfaceEffect",         "CExtAnimCutsceneEvent",         "ROOT",
        [("type","ESceneEventSurfacePostFXType"), ("worldPos","Bool"), ("position","Vector"),
         ("radius","Float"), ("fadeInTime","Float"), ("fadeOutTime","Float"), ("durationTime","Float")]),
    ("CExtAnimCutsceneDisableClothEvent",     "CExtAnimEvent",                 "ROOT",
        [("weight","Float"), ("blendTime","Float")]),
    ("CExtAnimCutsceneDisableDangleEvent",    "CExtAnimEvent",                 "ROOT",
        [("weight","Float")]),
    ("CExtAnimCutsceneResetClothAndDangleEvent","CExtAnimEvent",               "ROOT",
        [("forceRelaxedState","Bool")]),
    ("CExtAnimCutsceneSetClippingPlanesEvent","CExtAnimEvent",                 "ROOT",
        [("nearPlaneDistance","ENearPlaneDistance"), ("farPlaneDistance","EFarPlaneDistance")]),
    ("CExtAnimCutsceneBokehDofEvent",         "CExtAnimEvent",                 "ROOT",
        [("bokehDofParams","SBokehDofParams")]),
    ("CExtAnimCutsceneBokehDofBlendEvent",    "CExtAnimDurationEvent",         "ROOT",
        [("bokehDofParamsStart","SBokehDofParams"), ("bokehDofParamsEnd","SBokehDofParams")]),
    ("CExtAnimCutsceneQuestEvent",            "CExtAnimEvent",                 "ROOT",
        [("cutsceneName","String")]),
    ("CExtAnimCutsceneBreakEvent",            "CExtAnimEvent",                 "ROOT", []),
    # ── ENTRY: animation-level events ───────────────────────────────────────
    ("CExtAnimCutsceneBodyPartEvent",         "CExtAnimEvent",                 "ENTRY",
        [("appearance","CName")]),
    ("CExtAnimCutsceneDialogEvent",           "CExtAnimEvent",                 "ENTRY", []),
    ("CExtAnimDialogKeyPoseMarker",           "CExtAnimEvent",                 "ENTRY", []),
    ("CExtAnimDialogKeyPoseDuration",         "CExtAnimDurationEvent",         "ENTRY",
        [("transition","Bool"), ("keyPose","Bool")]),
    ("CExtAnimDisableDialogLookatEvent",      "CExtAnimDurationEvent",         "ENTRY",
        [("speed","Float")]),
    ("CExtAnimScriptEvent",                   "CExtAnimEvent",                 "ENTRY", []),
    ("CExtAnimScriptDurationEvent",           "CExtAnimDurationEvent",         "ENTRY", []),
    ("CExtAnimRaiseEventEvent",               "CExtAnimEvent",                 "ENTRY",
        [("eventToBeRaisedName","CName"), ("forceRaiseEvent","Bool")]),
    ("CExtAnimEffectEvent",                   "CExtAnimEvent",                 "ENTRY",
        [("effectName","CName"), ("action","EAnimEffectAction")]),
    ("CExtAnimEffectDurationEvent",           "CExtAnimDurationEvent",         "ENTRY",
        [("effectName","CName")]),
    ("CExtAnimMorphEvent",                    "CExtAnimDurationEvent",         "ENTRY",
        [("morphComponentId","CName"), ("invertWeight","Bool"), ("useCurve","Bool")]),
    ("CExtAnimItemEvent",                     "CExtAnimEvent",                 "ENTRY",
        [("category","CName"), ("itemName_optional","CName"), ("action","EItemAction"), ("itemGetting","EGettingItem")]),
    ("CExtAnimItemEffectEvent",               "CExtAnimEvent",                 "ENTRY",
        [("effectName","CName"), ("itemSlot","CName"), ("action","EItemEffectAction")]),
    ("CExtAnimItemEffectDurationEvent",       "CExtAnimDurationEvent",         "ENTRY",
        [("effectName","CName"), ("itemSlot","CName")]),
    ("CExtAnimItemSyncEvent",                 "CExtAnimEvent",                 "ENTRY",
        [("equipSlot","CName"), ("holdSlot","CName"), ("action","EItemLatentAction")]),
    ("CExtAnimItemSyncDurationEvent",         "CExtAnimDurationEvent",         "ENTRY",
        [("equipSlot","CName"), ("holdSlot","CName"), ("action","EItemLatentAction")]),
    ("CExtAnimItemSyncWithCorrectionEvent",   "CExtAnimDurationEvent",         "ENTRY",
        [("equipSlot","CName"), ("holdSlot","CName"), ("action","EItemLatentAction"), ("correctionBone","CName")]),
    ("CExtAnimItemAnimationEvent",            "CExtAnimEvent",                 "ENTRY",
        [("itemCategory","CName"), ("itemAnimationName","CName")]),
    ("CExtAnimItemBehaviorEvent",             "CExtAnimEvent",                 "ENTRY",
        [("itemCategory","CName"), ("event","CName")]),
    ("CExtAnimDropItemEvent",                 "CExtAnimEvent",                 "ENTRY",
        [("action","EDropAction")]),
    ("CExtAnimReattachItemEvent",             "CExtAnimDurationEvent",         "ENTRY",
        [("item","CName"), ("targetSlot","CName")]),
    ("CExtAnimAttackEvent",                   "CExtAnimEvent",                 "ENTRY",
        [("soundAttackType","CName")]),
    ("CExtAnimHitEvent",                      "CExtAnimEvent",                 "ENTRY",
        [("hitLevel","Uint32")]),
    ("CExtAnimSoundEvent",                    "CExtAnimEvent",                 "ENTRY",
        [("soundEventName","StringAnsi"), ("maxDistance","Float"), ("bone","CName"),
         ("filter","Bool"), ("filterCooldown","Float"), ("useDistanceParameter","Bool")]),
    ("CExtAnimFootstepEvent",                 "CExtAnimSoundEvent",            "ENTRY",
        [("fx","Bool"), ("customFxName","CName")]),
    ("CExtAnimLookAtEvent",                   "CExtAnimDurationEvent",         "ENTRY",
        [("level","ELookAtLevel")]),
    ("CExtAnimMaterialBasedFxEvent",          "CExtAnimEvent",                 "ENTRY",
        [("bone","CName"), ("vfxKickup","Bool"), ("vfxFootstep","Bool")]),
    ("CExtAnimProjectileEvent",               "CExtAnimEvent",                 "ENTRY",
        [("castPosition","EProjectileCastPosition"), ("boneName","CName")]),
    ("CExtAnimGameplayMimicEvent",            "CExtAnimDurationEvent",         "ENTRY",
        [("animation","CName")]),
    ("CExtAnimOnSlopeEvent",                  "CExtAnimDurationEvent",         "ENTRY",
        [("slopeAngle","Float")]),
    ("CExtAnimExplorationEvent",              "CExtAnimDurationEvent",         "ENTRY", []),
    ("CExtAnimComboEvent",                    "CExtAnimDurationEvent",         "ENTRY", []),
    ("CExtAnimLocationAdjustmentEvent",       "CExtAnimDurationEvent",         "ENTRY",
        [("locationAdjustmentVar","CName"), ("adjustmentActiveVar","CName")]),
    ("CExtAnimRotationAdjustmentEvent",       "CExtAnimDurationEvent",         "ENTRY",
        [("rotationAdjustmentVar","CName")]),
    ("CExtAnimRotationAdjustmentLocationBasedEvent","CExtAnimDurationEvent",   "ENTRY",
        [("locationAdjustmentVar","CName"), ("targetLocationVar","CName"), ("adjustmentActiveVar","CName")]),
]

_EVENT_SCHEMA_BY_CLASS = {s[0]: s for s in _CUTSCENE_EVENT_SCHEMA}

# EnumProperty items lists (module-level so Blender can cache them)
_ANIM_EVENT_ENUM_ITEMS_ROOT  = [(c, c, f"↑ {b}") for c,b,s,_ in _CUTSCENE_EVENT_SCHEMA if s=="ROOT"]
_ANIM_EVENT_ENUM_ITEMS_ENTRY = [(c, c, f"↑ {b}") for c,b,s,_ in _CUTSCENE_EVENT_SCHEMA if s=="ENTRY"]
_ANIM_EVENT_ENUM_ITEMS_ALL   = _ANIM_EVENT_ENUM_ITEMS_ROOT + _ANIM_EVENT_ENUM_ITEMS_ENTRY


def _event_schema_has_duration(class_name):
    """True if the event class (or its base) is a duration event."""
    entry = _EVENT_SCHEMA_BY_CLASS.get(class_name)
    if not entry:
        return "Duration" in class_name
    base = entry[1]
    return "Duration" in class_name or "Duration" in base


class WITCH_UL_CutsceneDialogList(UIList):
    bl_idname = "WITCH_UL_CutsceneDialogList"
    layout_type = "DEFAULT"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index, flt_flag):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            actor_split = row.split(factor=0.16, align=True)
            actor_split.label(text=item.actor or "?", icon='OUTLINER_OB_SPEAKER')

            text_split = actor_split.split(factor=0.86, align=True)
            text_split.label(text=item.line_text or "<text not resolved>", icon='FONT_DATA', translate=False)

            frame_row = text_split.row(align=True)
            frame_row.alignment = 'RIGHT'
            if item.start_frame or item.end_frame:
                frame_row.label(text=f"{item.start_frame}-{item.end_frame}")
            if item.imported_sound:
                frame_row.label(text="", icon='SOUND')
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


def _count_cutscene_anim_parts(context, anim_item):
    """Count NLA strips on the cutscene_anim track for the actor+component of anim_item."""
    actor_name = str(getattr(anim_item, "actor_name", "") or "").strip().lower()
    component = str(getattr(anim_item, "component_name", "") or "").strip().lower()
    full_name = str(getattr(anim_item, "full_name", "") or "").strip()
    scene = getattr(context, "scene", None)
    if scene is None or not actor_name:
        return 1
    count = 0
    track_names = {export_cutscene.CUTSCENE_TRACK_NAME, export_cutscene.CUTSCENE_FACE_TRACK_NAME}
    for obj in scene.objects:
        if getattr(obj, "type", None) != 'ARMATURE':
            continue
        obj_actor = str(obj.get("cutscene_actor_name", "") or "").strip().lower()
        if obj_actor != actor_name:
            continue
        anim_data = getattr(obj, "animation_data", None)
        if anim_data is None:
            continue
        for track in getattr(anim_data, "nla_tracks", []) or []:
            if str(getattr(track, "name", "") or "") not in track_names:
                continue
            for strip in getattr(track, "strips", []) or []:
                strip_action = getattr(strip, "action", None)
                if strip_action is None:
                    continue
                stored = str(strip_action.get(export_cutscene.CUTSCENE_ANIMATION_NAME_PROP, "") or "").strip()
                if stored == full_name:
                    count += 1
    return max(1, count)


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
                part_count = _count_cutscene_anim_parts(context, item)
                if part_count > 1:
                    row.label(text=f"×{part_count}", icon='BLANK1')
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


def _cutscene_scene_fps(scene):
    render = getattr(scene, "render", None)
    fps = float(getattr(render, "fps", 30.0) or 30.0)
    fps_base = float(getattr(render, "fps_base", 1.0) or 1.0)
    if fps_base <= 0.0:
        fps_base = 1.0
    return fps / fps_base


def _dialog_default_duration_frames(line_text, fps):
    text_len = len(str(line_text or "").strip())
    if text_len <= 0:
        return int(round(max(1.0, fps * 2.5)))
    seconds = max(1.5, min(8.0, text_len / 14.0))
    return int(round(max(1.0, seconds * fps)))


def _cutscene_dialog_line_id(dialog_data):
    line_id = str(dialog_data.get("line_id", "") or "").strip()
    if line_id:
        return line_id
    try:
        line_index = int(dialog_data.get("line_index", 0) or 0)
    except (TypeError, ValueError):
        line_index = 0
    return str(line_index) if line_index else ""


def _cutscene_dialog_int32(value):
    try:
        int_value = int(value or 0)
    except (TypeError, ValueError):
        return 0
    if int_value < -2147483648 or int_value > 2147483647:
        return 0
    return int_value


def _numeric_voice_candidate(value):
    value = str(value or "").strip().replace("\\", "/")
    if not value:
        return ""
    stem = os.path.splitext(os.path.basename(value))[0]
    return stem if stem.isdigit() else ""


def _cutscene_dialog_voice_id_candidates(dialog_data):
    candidates = []
    for value in (
        _numeric_voice_candidate(dialog_data.get("voice_file", "")),
        _cutscene_dialog_line_id(dialog_data),
    ):
        value = str(value or "").strip()
        if value and value.isdigit() and value not in candidates:
            candidates.append(value)
    return candidates


def _cutscene_dialog_strip_props(filepath, dialog_data, item=None):
    line_id = str(getattr(item, "line_id", "") or _cutscene_dialog_line_id(dialog_data))
    line_text = str(getattr(item, "line_text", "") or dialog_data.get("line_text", "") or "")
    sound_event = str(getattr(item, "sound_event", "") or dialog_data.get("sound_event", "") or "")
    text_language = dialog_language.get_active_text_language()
    return {
        dialog_language.DIALOG_SUBTITLE_TEXT_PROP: line_text,
        dialog_language.DIALOG_SUBTITLE_LINE_ID_PROP: line_id,
        dialog_language.DIALOG_SUBTITLE_SOURCE_PROP: "w2cutscene",
        dialog_language.DIALOG_SUBTITLE_SOURCE_PATH_PROP: str(filepath or ""),
        dialog_language.DIALOG_SUBTITLE_LANGUAGE_PROP: text_language,
        import_cutscene.CUTSCENE_DIALOG_AUDIO_PROP: True,
        import_cutscene.CUTSCENE_DIALOG_LINE_ID_PROP: line_id,
        import_cutscene.CUTSCENE_DIALOG_TEXT_PROP: line_text,
        import_cutscene.CUTSCENE_DIALOG_SOUND_EVENT_PROP: sound_event,
        import_cutscene.CUTSCENE_DIALOG_SOURCE_PATH_PROP: str(filepath or ""),
    }


def _find_cutscene_animation_strip_start(scene, animation_entry):
    if animation_entry is None:
        return None
    try:
        source_index = int(getattr(animation_entry, "source_index", -1))
    except Exception:
        source_index = -1
    full_name = str(getattr(animation_entry, "full_name", "") or "").strip()
    filepath = str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or "").strip()
    track_names = {import_cutscene.CUTSCENE_TRACK_NAME, import_cutscene.CUTSCENE_FACE_TRACK_NAME}
    starts = []

    for obj in getattr(scene, "objects", []) or []:
        if getattr(obj, "type", None) != 'ARMATURE':
            continue
        anim_data = getattr(obj, "animation_data", None)
        if anim_data is None:
            continue
        for track in getattr(anim_data, "nla_tracks", []) or []:
            if str(getattr(track, "name", "") or "") not in track_names:
                continue
            for strip in getattr(track, "strips", []) or []:
                action = getattr(strip, "action", None)
                if action is None:
                    continue
                try:
                    action_index = int(action.get(import_cutscene.CUTSCENE_SOURCE_INDEX_PROP, -1) or -1)
                except Exception:
                    action_index = -1
                if action_index != source_index:
                    continue
                action_name = str(action.get(import_cutscene.CUTSCENE_ANIMATION_NAME_PROP, "") or "").strip()
                if full_name and action_name and action_name != full_name:
                    continue
                action_path = str(action.get(import_cutscene.CUTSCENE_SOURCE_PATH_PROP, "") or "").strip()
                if filepath and action_path and not _same_filesystem_path(action_path, filepath):
                    continue
                starts.append(float(getattr(strip, "frame_start", 0.0) or 0.0))

    return min(starts) if starts else None


def _collect_cutscene_dialog_event_frames(scene):
    fps = _cutscene_scene_fps(scene)
    dialog_events = []
    for event in list(getattr(scene, "witcher_cutscene_event_items", [])):
        if "DialogEvent" not in str(getattr(event, "event_type", "") or ""):
            continue

        event_scope = str(getattr(event, "event_scope", "") or "").upper()
        event_fps = fps
        start_frame = float(getattr(event, "start_time", 0.0) or 0.0) * fps
        source_index = int(getattr(event, "source_index", -1) or -1)
        if event_scope == "ENTRY" and source_index >= 0:
            animation_entry = _find_loaded_cutscene_animation_entry(scene, source_index)
            if animation_entry is not None:
                anim_fps = float(getattr(animation_entry, "frames_per_second", 0.0) or 0.0)
                if anim_fps > 0.0:
                    event_fps = anim_fps
                strip_start = _find_cutscene_animation_strip_start(scene, animation_entry)
                start_frame = float(strip_start or 0.0) + (float(getattr(event, "start_time", 0.0) or 0.0) * event_fps)

        duration_frames = int(round(float(getattr(event, "duration", 0.0) or 0.0) * event_fps))
        dialog_events.append({
            "frame": int(round(start_frame)),
            "duration_frames": max(0, duration_frames),
            "source_index": source_index,
            "event": event,
        })

    dialog_events.sort(key=lambda item: (int(item["frame"]), int(item["source_index"])))
    return dialog_events


def _finalize_cutscene_dialog_item_ranges(scene):
    fps = _cutscene_scene_fps(scene)
    items = sorted(
        list(getattr(scene, "witcher_cutscene_dialog_items", [])),
        key=lambda item: int(getattr(item, "start_frame", 0) or 0),
    )
    for idx, item in enumerate(items):
        start_frame = int(getattr(item, "start_frame", 0) or 0)
        end_frame = int(getattr(item, "end_frame", 0) or 0)
        default_end = start_frame + _dialog_default_duration_frames(getattr(item, "line_text", ""), fps)
        if end_frame <= start_frame:
            end_frame = default_end

        if not bool(getattr(item, "imported_sound", False)) and idx + 1 < len(items):
            next_start = int(getattr(items[idx + 1], "start_frame", 0) or 0)
            if next_start > start_frame:
                end_frame = min(end_frame, next_start - 1)

        item.end_frame = max(start_frame, int(end_frame))


def _populate_cutscene_dialog_items(scene, dialog_items, dialog_events):
    scene.witcher_cutscene_dialog_items.clear()
    fps = _cutscene_scene_fps(scene)
    for idx, dialog_data in enumerate(dialog_items):
        event_info = dialog_events[idx] if idx < len(dialog_events) else None
        start_frame = int(event_info["frame"]) if event_info is not None else 0
        duration_frames = int(event_info["duration_frames"]) if event_info is not None else 0
        line_text = str(dialog_data.get("line_text", "") or "")
        if duration_frames <= 0:
            duration_frames = _dialog_default_duration_frames(line_text, fps)

        item = scene.witcher_cutscene_dialog_items.add()
        item.actor = str(dialog_data.get("actor", "") or "")
        item.voice_file = str(dialog_data.get("voice_file", "") or "")
        item.sound_event = str(dialog_data.get("sound_event", "") or "")
        item.line_id = _cutscene_dialog_line_id(dialog_data)
        item.line_index = _cutscene_dialog_int32(dialog_data.get("line_index", 0))
        item.line_text = line_text
        item.scene_path = str(dialog_data.get("scene_path", "") or "")
        item.start_frame = start_frame
        item.end_frame = start_frame + max(1, duration_frames)
        item.imported_sound = False

    _finalize_cutscene_dialog_item_ranges(scene)


def _resolve_cutscene_dialog_item_text(scene, item, language=""):
    line_id = str(getattr(item, "line_id", "") or getattr(item, "line_index", "") or "").strip()
    if not line_id:
        return ""

    cutscene_filepath = str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or "").strip()
    source_scene_path = ""
    linked_scene_path = str(getattr(item, "scene_path", "") or "").strip()
    if linked_scene_path:
        try:
            source_scene_path = import_cutscene.resolve_cutscene_linked_scene_file(linked_scene_path, cutscene_filepath)
        except Exception:
            log.debug("Could not resolve linked scene path for dialog text: %s", linked_scene_path, exc_info=True)
    return dialog_language.resolve_localized_text(line_id, source_scene_path or cutscene_filepath, language=language)


def refresh_cutscene_dialog_language(context, refresh_audio=False):
    scene = getattr(context, "scene", None) if context is not None else None
    if scene is None:
        return 0

    language = dialog_language.get_active_text_language(context)
    updated = 0
    for item in getattr(scene, "witcher_cutscene_dialog_items", []) or []:
        text = _resolve_cutscene_dialog_item_text(scene, item, language=language)
        if str(getattr(item, "line_text", "") or "") != text:
            item.line_text = text
            updated += 1

        line_id = str(getattr(item, "line_id", "") or "").strip()
        if line_id:
            try:
                for strip in import_cutscene._iter_cutscene_dialog_audio_strips(
                    scene,
                    source_path=str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or ""),
                    line_id=line_id,
                ):
                    strip[dialog_language.DIALOG_SUBTITLE_TEXT_PROP] = text
                    strip[dialog_language.DIALOG_SUBTITLE_LANGUAGE_PROP] = language
                    strip[import_cutscene.CUTSCENE_DIALOG_TEXT_PROP] = text
            except Exception:
                log.debug("Could not update dialog strip text for line %s.", line_id, exc_info=True)

    if refresh_audio and str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or "").strip():
        try:
            result = _load_cutscene_dialogs_into_scene(context)
            updated += int(result.get("loaded", 0) or 0) + int(result.get("sound_loaded", 0) or 0)
        except Exception:
            log.warning("Could not refresh cutscene dialog audio/lipsync for the new language.", exc_info=True)

    return updated


def _load_cutscene_dialogs_into_scene(context):
    from ..ui.ui_voice import load_voice_and_lipsync

    scene = context.scene
    filepath = str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or "").strip()
    if not filepath:
        raise RuntimeError("No cutscene loaded.")

    dialog_items = import_cutscene.load_cutscene_dialog_items(filepath)
    dialog_events = _collect_cutscene_dialog_event_frames(scene)
    _populate_cutscene_dialog_items(scene, dialog_items, dialog_events)

    if not dialog_items:
        return {"loaded": 0, "skipped": 0, "total": 0, "sound_loaded": 0}

    loaded = 0
    skipped = 0
    sound_loaded = 0
    for idx, dialog_data in enumerate(dialog_items):
        item = scene.witcher_cutscene_dialog_items[idx] if idx < len(scene.witcher_cutscene_dialog_items) else None
        line_id = str(getattr(item, "line_id", "") or _cutscene_dialog_line_id(dialog_data)).strip()

        voicetag = str(dialog_data.get("actor", "") or "")
        actor_obj = _find_actor_obj_by_voicetag(scene, voicetag)
        at_frame = float(getattr(item, "start_frame", 0) if item is not None else 0)
        strip_props = _cutscene_dialog_strip_props(filepath, dialog_data, item=item)

        imported_line = False
        soundstrip = None
        try:
            if line_id:
                import_cutscene.remove_cutscene_dialog_audio_strips(
                    scene,
                    source_path=filepath,
                    line_id=line_id,
                )
        except Exception:
            log.debug("Could not remove existing dialog audio for line %s", line_id, exc_info=True)

        for voice_id in _cutscene_dialog_voice_id_candidates(dialog_data):
            try:
                soundstrip = load_voice_and_lipsync(
                    str(voice_id),
                    actor=actor_obj,
                    context=context,
                    at_frame=at_frame,
                    strip_props=strip_props,
                )
                imported_line = True
                break
            except Exception as exc:
                log.warning("Failed to load voice line %s for actor %s: %s", voice_id, voicetag, exc)

        if soundstrip is None and str(dialog_data.get("sound_event", "") or "").strip():
            try:
                soundstrip = import_cutscene.import_sound_event_to_timeline(
                    context,
                    str(dialog_data.get("sound_event", "") or ""),
                    frame_start=at_frame,
                    source_path=filepath,
                    line_id=line_id,
                    line_text=str(dialog_data.get("line_text", "") or ""),
                    strip_props=strip_props,
                )
                imported_line = imported_line or soundstrip is not None
            except Exception as exc:
                log.warning(
                    "Failed to load dialog sound event %s for actor %s: %s",
                    dialog_data.get("sound_event", ""),
                    voicetag,
                    exc,
                )

        if soundstrip is not None and item is not None:
            item.imported_sound = True
            sound_loaded += 1
            try:
                item.end_frame = max(
                    int(getattr(item, "end_frame", 0) or 0),
                    int(math.ceil(float(getattr(soundstrip, "frame_final_end", at_frame) or at_frame))),
                )
            except Exception:
                pass

        if imported_line:
            loaded += 1
        else:
            skipped += 1

    _finalize_cutscene_dialog_item_ranges(scene)
    return {"loaded": loaded, "skipped": skipped, "total": len(dialog_items), "sound_loaded": sound_loaded}


class WITCH_OT_LoadCutsceneDialogs(Operator):
    bl_idname = "witcher.load_cutscene_dialogs"
    bl_label = "Load Dialogs"
    bl_description = (
        "Read CStorySceneLine fields from the linked .w2scene, then load each "
        "voiceFileName/dialogLine + lipsync onto the matching voicetag at the time given by the cutscene's "
        "CExtAnimCutsceneDialogEvent markers"
    )

    def execute(self, context):
        scene = context.scene
        filepath = str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or "").strip()
        if not filepath:
            self.report({'WARNING'}, "No cutscene loaded.")
            return {'CANCELLED'}

        try:
            stats = _load_cutscene_dialogs_into_scene(context)
        except Exception as exc:
            log.exception("Failed to load dialog items for %s", filepath)
            self.report({'ERROR'}, f"Dialog load failed: {exc}")
            return {'CANCELLED'}

        loaded = int(stats.get("loaded", 0) or 0)
        skipped = int(stats.get("skipped", 0) or 0)
        total = int(stats.get("total", 0) or 0)
        sound_loaded = int(stats.get("sound_loaded", 0) or 0)
        if total <= 0:
            self.report({'INFO'}, "No dialog lines found in linked .w2scene.")
            return {'FINISHED'}

        msg = f"Loaded {loaded} voice line(s)"
        if sound_loaded:
            msg += f", {sound_loaded} sound strip(s)"
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
    _set_cutscene_burned_audio_scene_state(scene, event_name="", item_path="")
    _set_cutscene_export_metadata_scene_state(scene, synced=False)
    if hasattr(scene, "witcher_cutscene_last_import_seconds"):
        scene.witcher_cutscene_last_import_seconds = 0.0
    if hasattr(scene, "witcher_loaded_w2cutscene_path"):
        scene.witcher_loaded_w2cutscene_path = ""
    if hasattr(scene, "witcher_w2scene_active_cutscene_path"):
        scene.witcher_w2scene_active_cutscene_path = ""

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


def _get_loaded_cutscene_burned_audio_strip(scene):
    filepath = str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or "").strip()
    if not filepath:
        return None
    return import_cutscene.find_cutscene_burned_audio_strip(scene, source_path=filepath)

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


def _same_filesystem_path(path_a, path_b):
    path_a = str(path_a or "").strip()
    path_b = str(path_b or "").strip()
    if not path_a or not path_b:
        return False
    try:
        return os.path.normcase(os.path.normpath(path_a)) == os.path.normcase(os.path.normpath(path_b))
    except Exception:
        return path_a == path_b

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
    _sync_cutscene_burned_audio_state(scene, filepath, _cutscene, cutscene_data=cutscene_data)
    _sync_cutscene_export_metadata_state(scene, _cutscene)
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
        item.tag = str(actor_data.get("tag", "") or "")
        item.voice_tag = str(actor_data.get("voice_tag", "") or "")
        item.template_path = str(actor_data["template_path"])
        item.appearance_name = str(actor_data["appearance_name"])
        item.actor_type = str(actor_data["actor_type"])
        item.final_position = str(actor_data.get("final_position", "") or "")
        item.kill_me = bool(actor_data.get("kill_me", False))
        item.use_mimic = bool(actor_data["use_mimic"])
        item.anim_final_pos = str(actor_data.get("anim_final_pos", "") or "")
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

class ButtonOperatorImportW2cutscene(Operator, ImportHelper):
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
    auto_apply_dialog: BoolProperty(
        name="Auto Apply Dialog",
        default=True,
        description="Load linked dialog voice strips and lipsync onto matching actors after import",
    )
    import_burned_audio: BoolProperty(
        name="Import Burned Track",
        default=True,
        description="Import the cutscene burned-track audio strip into the Blender sequencer",
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
        auto_box = settings_box.box()
        auto_box.label(text="Auto Apply", icon='PREFERENCES')
        auto_box.prop(self, "auto_apply_animations")
        auto_box.prop(self, "auto_apply_dialog")
        auto_box.prop(self, "import_burned_audio")

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
                anim_box.label(text="Auto-apply uses matching actors already in scene or being loaded.", icon='INFO')
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
                import_burned_audio=self.import_burned_audio,
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
        if hasattr(context.scene, "witcher_w2scene_active_cutscene_path"):
            context.scene.witcher_w2scene_active_cutscene_path = ""
        dialog_loaded_count = 0
        dialog_skipped_count = 0
        dialog_total_count = 0
        dialog_sound_loaded_count = 0
        if self.auto_apply_dialog:
            try:
                dialog_stats = _load_cutscene_dialogs_into_scene(context)
                dialog_loaded_count = int(dialog_stats.get("loaded", 0) or 0)
                dialog_skipped_count = int(dialog_stats.get("skipped", 0) or 0)
                dialog_total_count = int(dialog_stats.get("total", 0) or 0)
                dialog_sound_loaded_count = int(dialog_stats.get("sound_loaded", 0) or 0)
            except Exception as exc:
                log.exception("Failed to auto-apply cutscene dialog for %s", self.filepath)
                self.report({'WARNING'}, f"Cutscene imported, but dialog auto-apply failed: {exc}")
        burned_audio_info = dict(getattr(cutscene_data, "burned_audio_info", {}) or {})
        status_parts = []
        if self.auto_apply_dialog:
            if dialog_total_count > 0:
                dialog_text = f"loaded {dialog_loaded_count} dialog line(s)"
                if dialog_sound_loaded_count:
                    dialog_text += f", {dialog_sound_loaded_count} sound strip(s)"
                if dialog_skipped_count:
                    dialog_text += f" ({dialog_skipped_count} skipped)"
                status_parts.append(dialog_text)
            else:
                status_parts.append("no dialog lines found")
        if self.import_burned_audio and burned_audio_info:
            status_parts.append("burned track imported")
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
            ) + (f" {'; '.join(status_parts)}." if status_parts else ""),
        )
        return {'FINISHED'}
    def invoke(self, context, event):
        UNCOOK_PATH = os.path.join(get_uncook_path(context),"animations\\")
        if os.path.exists(UNCOOK_PATH):
            self.filepath = UNCOOK_PATH if self.filepath == '' else self.filepath
        return ImportHelper.invoke(self, context, event)

class WITCH_OT_ReopenCutsceneImportDialog(Operator):
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


class WITCH_OT_ImportCutsceneBurnedAudio(Operator):
    bl_idname = "witcher.import_cutscene_burned_audio"
    bl_label = "Import Burned Track"
    bl_description = "Import the cutscene burned-track audio strip into the Blender sequencer"

    def execute(self, context):
        scene = context.scene
        filepath = str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or "").strip()
        if not filepath:
            self.report({'WARNING'}, "No cutscene loaded.")
            return {'CANCELLED'}

        try:
            burned_audio_info = import_cutscene.import_cutscene_burned_audio_track(context, filepath=filepath)
        except Exception as exc:
            log.exception("Failed to import cutscene burned audio for %s", filepath)
            self.report({'ERROR'}, f"Failed to import burned track: {exc}")
            return {'CANCELLED'}

        if not burned_audio_info:
            self.report({'WARNING'}, "No burned track is defined for this cutscene.")
            return {'CANCELLED'}

        _set_cutscene_burned_audio_scene_state(
            scene,
            event_name=burned_audio_info.get("event_name", ""),
            item_path=burned_audio_info.get("item_path", ""),
        )
        self.report({'INFO'}, f"Imported burned track '{burned_audio_info.get('event_name', '')}'.")
        return {'FINISHED'}


class WITCH_OT_RemoveCutsceneBurnedAudio(Operator):
    bl_idname = "witcher.remove_cutscene_burned_audio"
    bl_label = "Remove Burned Track"
    bl_description = "Remove the imported cutscene burned-track audio strip from the Blender sequencer"

    def execute(self, context):
        scene = context.scene
        filepath = str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or "").strip()
        if not filepath:
            self.report({'WARNING'}, "No cutscene loaded.")
            return {'CANCELLED'}

        removed_count = int(import_cutscene.remove_cutscene_burned_audio_strips(scene, source_path=filepath) or 0)
        if removed_count <= 0:
            self.report({'WARNING'}, "No imported burned track found for this cutscene.")
            return {'CANCELLED'}

        self.report({'INFO'}, f"Removed {removed_count} burned track strip(s).")
        return {'FINISHED'}

class WITCH_OT_SetCutsceneAnimationLoaded(Operator):
    bl_idname = "witcher.set_cutscene_animation_loaded"
    bl_label = "Toggle Cutscene Animation"
    bl_description = "Activate or deactivate a cutscene animation (mutes/unmutes NLA strips)"
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

def _draw_event_list_item(self, layout, item):
    if self.layout_type in {'DEFAULT', 'COMPACT'}:
        row = layout.row(align=True)
        row.label(text=_get_cutscene_event_label(item), icon=_event_type_icon(item.event_type))
        # Short class name badge
        cls_badge = row.row(align=True)
        cls_badge.enabled = False
        cls_badge.scale_x = 0.7
        cls_short = str(item.event_type or "").replace("CExtAnimCutscene","").replace("CExtAnim","")
        cls_badge.label(text=cls_short)
        row.label(text=f"{item.start_time:.2f}s")
        if item.duration > 0.0:
            row.label(text=f"+{item.duration:.2f}s")
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


class WITCH_UL_ActorEntryEventList(UIList):
    """Events UIList filtered to animations belonging to the actor selected in the Actors tab."""
    bl_idname = "WITCH_UL_ActorEntryEventList"
    layout_type = "DEFAULT"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index, flt_flag):
        _draw_event_list_item(self, layout, item)

    def filter_items(self, context, data, propname):
        items = getattr(data, propname, [])
        scene = context.scene
        actor_filter = str(getattr(scene, "witcher_cs_actor_event_filter", "") or "").strip().lower()
        anims = list(getattr(scene, "witcher_cutscene_animation_items", []))
        actor_anim_indices = {
            int(getattr(a, "source_index", -1))
            for a in anims
            if str(getattr(a, "actor_name", "") or "").strip().lower() == actor_filter
        }
        flags = [
            self.bitflag_filter_item
            if (str(getattr(i, "event_scope", "") or "").upper() == "ENTRY"
                and int(getattr(i, "source_index", -1)) in actor_anim_indices)
            else 0
            for i in items
        ]
        return flags, []


_STORED_EVENT_FIELDS = frozenset({"eventName","startTime","animationName","duration","alwaysFiresEnd"})

def _draw_event_detail(layout, ev):
    detail_box = layout.box()
    event_type = str(getattr(ev, "event_type", "") or "")
    schema_entry = _EVENT_SCHEMA_BY_CLASS.get(event_type)

    # Class header with inheritance
    hdr = detail_box.row(align=True)
    hdr.label(text=event_type or "Event", icon=_event_type_icon(event_type))
    if schema_entry:
        hdr.label(text=f"↑ {schema_entry[1]}", icon='BLANK1')

    col = detail_box.column(align=True)
    col.use_property_split = True
    col.enabled = False

    col.prop(ev, "event_name")
    col.prop(ev, "start_time")
    col.prop(ev, "animation_name")
    if ev.duration > 0.0 or _event_schema_has_duration(event_type):
        col.prop(ev, "duration")
    if ev.track_name:
        col.prop(ev, "track_name")
    if ev.effect_name:
        col.prop(ev, "effect_name")
    if ev.appearance or 'BodyPart' in event_type or 'Appearance' in event_type:
        col.prop(ev, "appearance")

    # Informational: schema own props that we don't store in CutsceneEventItem
    if schema_entry:
        extra_names = [n for n, t in schema_entry[3]
                       if n not in _STORED_EVENT_FIELDS
                       and n not in ("appearance", "effectName", "effect")]
        if extra_names:
            info = detail_box.row()
            info.enabled = False
            info.label(text="+ " + ", ".join(extra_names), icon='INFO')

_ACTOR_CUSTOM_PROPS_DEFAULTS = {
    "cutscene_actor_name":         "",
    "cutscene_actor_tag":          "",
    "cutscene_actor_voice_tag":    "",
    "cutscene_actor_template":     "",
    "cutscene_actor_appearance":   "",
    "cutscene_actor_type":         "CAT_Actor",
    "cutscene_actor_final_position":  "",
    "cutscene_actor_kill_me":      False,
    "cutscene_actor_use_mimic":    False,
    "cutscene_actor_anim_final_pos": "",
}


def _ensure_actor_custom_props(obj):
    """Initialize missing actor custom props to their defaults (for safe layout.prop display)."""
    if obj is None:
        return
    for k, v in _ACTOR_CUSTOM_PROPS_DEFAULTS.items():
        if k not in obj:
            obj[k] = v


class WITCH_OT_CutsceneSelectActor(Operator):
    """Select this actor to view and edit its properties"""
    bl_idname = "witcher.cutscene_select_actor"
    bl_label = "Select Actor"
    bl_options = set()
    object_name: StringProperty(default="")

    def execute(self, context):
        scene = context.scene
        cur = str(getattr(scene, "witcher_cutscene_selected_actor_obj", "") or "")
        if cur == self.object_name:
            scene.witcher_cutscene_selected_actor_obj = ""
            if hasattr(scene, "witcher_cs_actor_event_filter"):
                scene.witcher_cs_actor_event_filter = ""
        else:
            scene.witcher_cutscene_selected_actor_obj = self.object_name
            obj = bpy.data.objects.get(self.object_name)
            _ensure_actor_custom_props(obj)
            if hasattr(scene, "witcher_cs_actor_event_filter"):
                actor_name = str(obj.get("cutscene_actor_name", "") or "").strip().lower() if obj else ""
                scene.witcher_cs_actor_event_filter = actor_name
        return {'FINISHED'}


class WITCH_OT_CutsceneRemoveActor(Operator):
    """Remove this armature from the cutscene (clears its cutscene tags)"""
    bl_idname = "witcher.cutscene_remove_actor"
    bl_label = "Remove Actor"
    bl_options = {'UNDO'}
    object_name: StringProperty(default="")

    def execute(self, context):
        scene = context.scene
        obj = bpy.data.objects.get(self.object_name)
        if obj is None:
            return {'CANCELLED'}
        import_cutscene._clear_cutscene_actor_tags(obj)
        for pn in ("cutscene_actor_tag", "cutscene_actor_voice_tag",
                   "cutscene_actor_final_position", "cutscene_actor_kill_me",
                   "cutscene_actor_anim_final_pos"):
            try:
                if pn in obj:
                    del obj[pn]
            except Exception:
                pass
        if str(getattr(scene, "witcher_cutscene_selected_actor_obj", "") or "") == self.object_name:
            scene.witcher_cutscene_selected_actor_obj = ""
        return {'FINISHED'}


class WITCH_OT_CutsceneRemoveAnimation(Operator):
    """Remove this animation entry from the cutscene list"""
    bl_idname = "witcher.cutscene_remove_animation"
    bl_label = "Remove Animation"
    bl_options = {'UNDO'}
    source_index: IntProperty(default=-1)

    def execute(self, context):
        scene = context.scene
        anims = list(getattr(scene, "witcher_cutscene_animation_items", []))
        for i, item in enumerate(anims):
            if int(getattr(item, "source_index", -2)) == self.source_index:
                scene.witcher_cutscene_animation_items.remove(i)
                break
        # Remove events tied to this animation
        events = list(getattr(scene, "witcher_cutscene_event_items", []))
        for i in reversed(range(len(events))):
            if int(getattr(events[i], "source_index", -2)) == self.source_index:
                scene.witcher_cutscene_event_items.remove(i)
        return {'FINISHED'}


def _add_event_class_items(self, context):
    """Dynamic enum items filtered by event_scope."""
    scope = str(getattr(self, "event_scope", "") or "")
    if scope == "ROOT":
        return _ANIM_EVENT_ENUM_ITEMS_ROOT
    if scope == "ENTRY":
        return _ANIM_EVENT_ENUM_ITEMS_ENTRY
    return _ANIM_EVENT_ENUM_ITEMS_ALL


class WITCH_OT_CutsceneAddEvent(Operator):
    """Add a new event to the cutscene or animation event list"""
    bl_idname = "witcher.cutscene_add_event"
    bl_label = "Add Event"
    bl_options = {'UNDO'}

    event_scope: EnumProperty(
        name="Scope",
        items=[("ROOT", "Cutscene (ROOT)", "Attached to the cutscene template"),
               ("ENTRY", "Animation (ENTRY)", "Attached to a specific animation entry")],
        default="ENTRY",
    )
    event_class: EnumProperty(name="Event Class", items=_add_event_class_items)
    source_index: IntProperty(name="Animation Source Index", default=-1)

    # CExtAnimEvent base fields
    event_name: StringProperty(name="eventName", default="")
    start_time: FloatProperty(name="startTime", default=0.0, min=0.0)
    animation_name: StringProperty(name="animationName", default="")
    report_to_script: BoolProperty(name="reportToScript", default=False)
    report_min_weight: FloatProperty(name="reportToScriptMinWeight", default=0.0, min=0.0, max=1.0)

    # CExtAnimDurationEvent
    duration: FloatProperty(name="duration", default=0.0, min=0.0)
    always_fires_end: BoolProperty(name="alwaysFiresEnd", default=False)

    # Own fields (stored in CutsceneEventItem)
    appearance: StringProperty(name="appearance (CName)", default="")
    effect_name: StringProperty(name="effectName / effect (CName)", default="")

    # Extra informational fields (displayed but not stored beyond event_type)
    extra_str1: StringProperty(name="", default="")
    extra_str2: StringProperty(name="", default="")

    def invoke(self, context, event):
        scene = context.scene
        # Pre-fill scope and source_index from context
        if self.event_scope == "ENTRY" and self.source_index < 0:
            anims = list(getattr(scene, "witcher_cutscene_animation_items", []))
            anim_ui_idx = getattr(scene, "witcher_cutscene_loaded_anim_index", 0)
            if 0 <= anim_ui_idx < len(anims):
                anim = anims[anim_ui_idx]
                if anim.source_index != -1:
                    self.source_index = anim.source_index
                    if not self.animation_name:
                        self.animation_name = str(anim.display_name or "")
        return context.window_manager.invoke_props_dialog(self, width=460)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True

        layout.prop(self, "event_scope")
        layout.prop(self, "event_class")

        schema_entry = _EVENT_SCHEMA_BY_CLASS.get(self.event_class)
        if schema_entry:
            base_row = layout.row()
            base_row.enabled = False
            base_row.label(text=f"↑ {schema_entry[1]}", icon='BLANK1')

        layout.separator()
        layout.label(text="Base fields  (CExtAnimEvent):", icon='PROPERTIES')
        layout.prop(self, "event_name")
        layout.prop(self, "start_time")
        layout.prop(self, "animation_name")
        layout.prop(self, "report_to_script")
        if self.report_to_script:
            layout.prop(self, "report_min_weight")

        if _event_schema_has_duration(self.event_class):
            layout.separator()
            layout.label(text="Duration fields  (CExtAnimDurationEvent):", icon='PROPERTIES')
            layout.prop(self, "duration")
            layout.prop(self, "always_fires_end")

        if schema_entry:
            own_props = schema_entry[3]
            if own_props:
                layout.separator()
                layout.label(text=f"Own fields  ({self.event_class}):", icon='PROPERTIES')
                for prop_name, prop_type in own_props:
                    if prop_name in ("duration", "alwaysFiresEnd"):
                        continue
                    if prop_name == "appearance":
                        layout.prop(self, "appearance")
                    elif prop_name in ("effectName", "effect"):
                        layout.prop(self, "effect_name")
                    else:
                        # Field not stored — show label only
                        row = layout.row()
                        row.enabled = False
                        row.label(text=f"{prop_name} ({prop_type}) — edit in game files", icon='INFO')

        if self.event_scope == "ENTRY":
            layout.separator()
            info = layout.row()
            info.enabled = False
            info.label(text=f"Animation source index: {self.source_index}", icon='INFO')

    def execute(self, context):
        scene = context.scene
        new_ev = scene.witcher_cutscene_event_items.add()
        new_ev.event_type = self.event_class
        new_ev.event_name = self.event_name
        new_ev.start_time = self.start_time
        new_ev.duration = self.duration
        new_ev.animation_name = self.animation_name
        new_ev.appearance = self.appearance
        new_ev.effect_name = self.effect_name
        new_ev.always_fires_end = self.always_fires_end
        new_ev.event_scope = self.event_scope
        new_ev.source_index = self.source_index
        return {'FINISHED'}


_ECutsceneActorType_ITEMS = [
    ("CAT_Actor",  "CAT_Actor",  "Animated actor"),
    ("CAT_Prop",   "CAT_Prop",   "Animated prop"),
    ("CAT_Camera", "CAT_Camera", "Cutscene camera"),
    ("CAT_None",   "CAT_None",   "No type"),
]
_ECutsceneActorType_VALUES = [item[0] for item in _ECutsceneActorType_ITEMS]
_ECutsceneActorType_INDEX = {v: i for i, v in enumerate(_ECutsceneActorType_VALUES)}


def _actor_type_get(self):
    val = str(self.get("cutscene_actor_type", "CAT_Actor") or "CAT_Actor")
    return _ECutsceneActorType_INDEX.get(val, 0)


def _actor_type_set(self, value):
    if 0 <= value < len(_ECutsceneActorType_VALUES):
        self["cutscene_actor_type"] = _ECutsceneActorType_VALUES[value]


def _draw_cutscene_template_tab(layout, scene):
    layout.use_property_split = False
    layout.use_property_decorate = False

    path_row = layout.row()
    path_row.label(text=str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or ""), icon='FILE')

    burned_box = layout.box()
    burned_header = burned_box.row(align=True)
    burned_header.label(text="Burned Track", icon='SOUND')
    burned_strip = _get_loaded_cutscene_burned_audio_strip(scene)
    burned_event = str(getattr(scene, "witcher_cutscene_burned_audio_event", "") or "").strip()
    burned_item_path = str(getattr(scene, "witcher_cutscene_burned_audio_item_path", "") or "").strip()
    if burned_strip is not None:
        burned_header.label(text="Loaded", icon='CHECKMARK')
        burned_header.operator(WITCH_OT_ImportCutsceneBurnedAudio.bl_idname, text="", icon='FILE_REFRESH')
        burned_header.operator(WITCH_OT_RemoveCutsceneBurnedAudio.bl_idname, text="", icon='X')
    elif burned_event:
        burned_header.label(text="Not Loaded", icon='INFO')
        burned_header.operator(WITCH_OT_ImportCutsceneBurnedAudio.bl_idname, text="Import", icon='IMPORT')
    else:
        burned_header.label(text="None", icon='INFO')

    burned_box.prop(scene, "witcher_cutscene_burned_audio_default_volume", text="Default Import Volume", slider=True)
    if burned_event:
        burned_box.label(text=f"Event: {burned_event}", icon='OUTLINER_OB_SPEAKER')
    else:
        burned_box.label(text="No burned track defined in this cutscene.", icon='INFO')
    if burned_item_path:
        burned_box.label(text=burned_item_path, icon='FILE')
    if burned_strip is not None and hasattr(burned_strip, "volume"):
        burned_box.prop(burned_strip, "volume", text="Sequencer Volume", slider=True)

    metadata_box = layout.box()
    metadata_box.label(text="Export Metadata", icon='TOOL_SETTINGS')
    metadata_box.prop(scene, "witcher_cutscene_point_tags", text="Point Tags")
    metadata_box.prop(scene, "witcher_cutscene_last_level_loaded", text="Last Level Loaded")
    metadata_box.prop(scene, "witcher_cutscene_used_in_files", text="Used In Files")
    metadata_box.prop(scene, "witcher_cutscene_burned_audio_event", text="Burned Audio Event")
    metadata_box.label(text="Use ';' to separate multiple tags or depot paths.", icon='INFO')

    template_box = layout.box()
    header = template_box.row(align=True)
    header.label(text="Template Fields", icon='PROPERTIES')
    header.prop(scene, "witcher_cutscene_show_unset_template_fields", text="Show Unset", toggle=True)

    _draw_imported_class_sections(
        template_box,
        list(getattr(scene, "witcher_cutscene_template_fields", [])),
        w3_types.CUTSCENE_CLASS_FIELD_SCHEMA,
        bool(getattr(scene, "witcher_cutscene_show_unset_template_fields", False)),
        "No set imported values.",
    )


def _draw_cutscene_actors_tab(layout, scene, context=None):
    actor_objects = [
        obj for obj in scene.objects
        if getattr(obj, "type", None) == 'ARMATURE'
        and str(obj.get("cutscene_actor_name", "") or "").strip()
    ]
    actor_objects.sort(key=lambda o: str(o.get("cutscene_actor_name", "") or o.name).lower())

    selected_obj_name = str(getattr(scene, "witcher_cutscene_selected_actor_obj", "") or "")
    selected_obj = bpy.data.objects.get(selected_obj_name) if selected_obj_name else None
    if selected_obj not in actor_objects:
        selected_obj = None

    # --- Actor list ---
    if actor_objects:
        list_box = layout.box()
        for obj in actor_objects:
            actor_name = str(obj.get("cutscene_actor_name", "") or obj.name)
            actor_type = str(obj.get("cutscene_actor_type", "") or "CAT_Actor")
            is_sel = (obj == selected_obj)
            type_icon = 'CAMERA_DATA' if actor_type == 'CAT_Camera' else ('OBJECT_DATA' if actor_type == 'CAT_Prop' else 'ARMATURE_DATA')
            row = list_box.row(align=True)
            sel_op = row.operator("witcher.cutscene_select_actor", text=actor_name, icon=type_icon, emboss=is_sel, depress=is_sel)
            sel_op.object_name = obj.name
            badge = row.row(align=True)
            badge.enabled = False
            badge.scale_x = 0.55
            badge.label(text=actor_type.replace("CAT_", ""))
            rm_op = row.operator("witcher.cutscene_remove_actor", text="", icon='X')
            rm_op.object_name = obj.name
    else:
        layout.label(text="No actors in cutscene.", icon='INFO')

    # --- Selected actor detail panel ---
    if selected_obj is not None:
        layout.separator(factor=0.3)
        detail_box = layout.box()
        actor_name = str(selected_obj.get("cutscene_actor_name", "") or selected_obj.name)
        detail_box.label(text=actor_name, icon='PROPERTIES')
        col = detail_box.column(align=True)

        col.prop(selected_obj, '["cutscene_actor_name"]', text="name")
        col.prop(selected_obj, '["cutscene_actor_tag"]', text="tag")
        col.prop(selected_obj, '["cutscene_actor_voice_tag"]', text="voiceTag")
        col.prop(selected_obj, '["cutscene_actor_template"]', text="template")
        col.prop(selected_obj, '["cutscene_actor_appearance"]', text="appearance")
        col.prop(selected_obj, '["cutscene_actor_final_position"]', text="finalPosition")
        col.prop(selected_obj, '["cutscene_actor_anim_final_pos"]', text="animationAtFinalPosition")
        col.prop(selected_obj, '["cutscene_actor_kill_me"]', text="killMe")
        col.prop(selected_obj, '["cutscene_actor_use_mimic"]', text="useMimic")

        col.separator()
        col.prop(selected_obj, "witcher_cutscene_actor_type", text="type")

        # Per-actor events (filter entry events by actor name)
        actor_name_key = str(selected_obj.get("cutscene_actor_name", "") or "").strip().lower()
        all_anims = list(getattr(scene, "witcher_cutscene_animation_items", []))
        all_events = list(getattr(scene, "witcher_cutscene_event_items", []))
        actor_anim_indices = {
            int(getattr(a, "source_index", -1))
            for a in all_anims
            if str(getattr(a, "actor_name", "") or "").strip().lower() == actor_name_key
        }
        actor_events = [
            e for e in all_events
            if str(getattr(e, "event_scope", "") or "").upper() == "ENTRY"
            and int(getattr(e, "source_index", -1)) in actor_anim_indices
        ]
        detail_box.separator(factor=0.5)
        ev_hdr = detail_box.row(align=True)
        ev_hdr.label(text=f"Events ({len(actor_events)})", icon='SEQUENCE')
        add_op = ev_hdr.operator("witcher.cutscene_add_event", text="", icon='ADD')
        add_op.event_scope = "ENTRY"
        add_op.source_index = min(actor_anim_indices, default=-1)
        if actor_events:
            detail_box.template_list(
                "WITCH_UL_ActorEntryEventList", "",
                scene, "witcher_cutscene_event_items",
                scene, "witcher_cs_actor_event_idx",
                rows=min(len(actor_events), 5),
            )
            ev_idx = getattr(scene, "witcher_cs_actor_event_idx", 0)
            if 0 <= ev_idx < len(all_events):
                ev = all_events[ev_idx]
                if int(getattr(ev, "source_index", -1)) in actor_anim_indices:
                    _draw_event_detail(detail_box, ev)
        else:
            detail_box.label(text="No events for this actor.", icon='INFO')

    layout.separator(factor=0.5)

    # --- Assign actor form ---
    assign_box = layout.box()
    assign_box.label(text="Assign Selected Armature", icon='ADD')
    actor_row = assign_box.row(align=True)
    actor_row.prop(scene, "witcher_cutscene_scratch_actor_name", text="Name")
    actor_row.prop(scene, "witcher_cutscene_scratch_actor_type", text="")
    assign_box.prop(scene, "witcher_cutscene_scratch_actor_template", text="Template")
    assign_box.prop(scene, "witcher_cutscene_scratch_actor_appearance", text="Appearance")
    opts_row = assign_box.row(align=True)
    opts_row.prop(scene, "witcher_cutscene_scratch_use_mimic", text="Mimic")
    opts_row.operator("witcher.cutscene_scratch_assign_actor", text="Assign Selected", icon='CHECKMARK')


def _draw_cutscene_anims_tab(layout, scene, context=None):
    anims = list(getattr(scene, "witcher_cutscene_animation_items", []))
    idx = getattr(scene, "witcher_cutscene_loaded_anim_index", 0)
    cs_selected = len(anims) > 0 and 0 <= idx < len(anims) and anims[idx].source_index == -1
    real_anims = [a for a in anims if a.source_index != -1]
    active_count = sum(1 for a in real_anims if _get_cutscene_animation_display_state(scene, a)["is_loaded"])

    # ── Animation list ────────────────────────────────────────────────────────
    if anims:
        layout.template_list(
            "WITCH_UL_LoadedAnimList", "",
            scene, "witcher_cutscene_animation_items",
            scene, "witcher_cutscene_loaded_anim_index",
            rows=min(len(anims), 8),
        )
        if not cs_selected and 0 <= idx < len(anims):
            anim = anims[idx]
            anim_state = _get_cutscene_animation_display_state(scene, anim)
            detail_box = layout.box()
            detail_row = detail_box.row(align=True)
            detail_row.label(text=_get_cutscene_animation_label(anim), icon='ACTION')
            # Active / inactive toggle
            if anim_state["is_loaded"]:
                op = detail_row.operator(WITCH_OT_SetCutsceneAnimationLoaded.bl_idname, text="Deactivate", icon='HIDE_ON')
                op.source_index = anim.source_index
                op.load = False
            else:
                op = detail_row.operator(WITCH_OT_SetCutsceneAnimationLoaded.bl_idname, text="Activate", icon='HIDE_OFF')
                op.source_index = anim.source_index
                op.load = True
            # Remove button
            rm_op = detail_row.operator("witcher.cutscene_remove_animation", text="", icon='X')
            rm_op.source_index = anim.source_index
            col = detail_box.column(align=True)
            if anim.actor_name:
                col.label(text=f"Actor: {anim.actor_name}", icon='ARMATURE_DATA')
            if anim.component_name:
                col.label(text=f"Component: {anim.component_name}", icon='BONE_DATA')
            if anim.frames_per_second:
                col.label(text=f"FPS: {anim.frames_per_second:.1f}   Frames: {anim.num_frames}")
            if anim.duration:
                col.label(text=f"Duration: {anim.duration:.3f}s")
    else:
        layout.label(text="No animations.", icon='INFO')

    if real_anims:
        layout.label(text=f"Active: {active_count}/{len(real_anims)}")

    layout.separator(factor=0.5)

    # Imported NLA clips
    import_box = layout.box()
    import_box.label(text="Loaded Import Clips", icon='NLA')
    import_candidates = _collect_cutscene_import_nla_candidates(scene)
    if import_candidates:
        for cand in import_candidates[:8]:
            row = import_box.row(align=True)
            label_name = cand["action_name"] or cand["strip_name"] or cand["track_name"]
            frame_start = int(round(cand["frame_start"]))
            frame_end = int(round(cand["frame_end"]))
            row.label(
                text=f"{cand['actor_name']} {cand['component']}: {label_name}  [{frame_start}-{frame_end}]",
                icon='ACTION',
            )
            op = row.operator("witcher.cutscene_use_import_nla_strip", text="Use", icon='CHECKMARK')
            op.actor_object_name = cand["actor_object_name"]
            op.source_object_name = cand["source_object_name"]
            op.track_name = cand["track_name"]
            op.strip_name = cand["strip_name"]
            op.source_frame_start = cand["frame_start"]
            op.component = cand["component"]
            op.mute_source = True
        if len(import_candidates) > 8:
            import_box.label(text=f"{len(import_candidates) - 8} more loaded import clips not shown.", icon='INFO')
    else:
        import_box.label(text="No active anim_import or mimic_import clips on cutscene actors.", icon='INFO')

    layout.separator(factor=0.5)

    # Add animation from an explicit action
    add_box = layout.box()
    add_hdr = add_box.row(align=True)
    add_hdr.label(text="Add Animation", icon='NLA')
    add_hdr.prop_search(scene, "witcher_cutscene_scratch_action_name", bpy.data, "actions", text="")
    opts_row = add_box.row(align=True)
    opts_row.prop(scene, "witcher_cutscene_scratch_component", text="")
    length_row = add_box.row(align=True)
    length_row.prop(scene, "witcher_cutscene_scratch_strip_length", text="Length (frames)")
    length_row.prop(scene, "witcher_cutscene_scratch_add_after_last", text="After Last")
    add_box.operator("witcher.cutscene_scratch_add_action", text="Add to Cutscene", icon='ADD')

    layout.separator(factor=0.3)
    validate_row = layout.row(align=True)
    validate_row.operator("witcher.cutscene_scratch_validate", text="Validate", icon='CHECKMARK')
    report = str(getattr(scene, "witcher_cutscene_scratch_validation_report", "") or "").strip()
    if report:
        for line in report.splitlines()[:6]:
            ico = 'ERROR' if line.startswith("ERROR") else ('INFO' if line.startswith("WARN") else 'CHECKMARK')
            layout.label(text=line[:100], icon=ico)


def _draw_camera_track_controls(layout, camera_arm):
    camera_bone = ensure_camera_track_properties(camera_arm, track_names=CAMERA_TRACK_NAMES)
    if camera_bone is None:
        layout.label(text="Camera rig is missing Camera_Node track properties.", icon='ERROR')
        return
    tracks_box = layout.box()
    tracks_box.label(text="Camera Tracks", icon='ANIM')
    tracks_box.operator("witcher.camera_set_dof_from_selected", text="DOF From Selected", icon='CAMERA_DATA')
    for track_name in CAMERA_TRACK_NAMES:
        if track_name in camera_bone:
            tracks_box.prop(camera_bone, f'["{track_name}"]', text=track_name)


def _draw_cutscene_camera_tab(layout, scene, context):
    # ── Primary workflow: Blender-native shots ─────────────────────────────
    shots_box = layout.box()
    shots_hdr = shots_box.row(align=True)
    shots_hdr.label(text="Shots", icon='CAMERA_DATA')
    shots_hdr.operator("witcher.cutscene_new_shot", text="New Shot", icon='ADD')
    shots_hdr.operator("witcher.cutscene_use_selected_camera_as_shot", text="Use Selected", icon='CHECKMARK')

    # List current shots from timeline markers
    shot_markers = [
        m for m in sorted(scene.timeline_markers, key=lambda m: m.frame)
        if getattr(m, "camera", None) is not None
        and m.camera.get("witcher_shot_index") is not None
    ]
    if shot_markers:
        for m in shot_markers:
            row = shots_box.row(align=True)
            row.label(text="", icon='SEQUENCE')
            row.label(text=m.camera.name)
            row.label(text=f"f{m.frame}")
    else:
        shots_box.label(text="No shots yet — press 'New Shot'", icon='INFO')

    shots_box.operator("witcher.camera_apply_blender_cameras_to_rig", text="Shots → Rig", icon='NLA_PUSHDOWN')

    layout.separator(factor=0.5)

    # ── Witcher rig tools (only if rig is in scene) ────────────────────────
    camera_arm = _cs_find_camera_armature(context)
    if camera_arm is None:
        # Offer quick rig import as a secondary action
        import_box = layout.box()
        import_box.label(text="Witcher Camera Rig", icon='ARMATURE_DATA')
        import_box.prop(scene, "witcher_cutscene_scratch_camera_repo_path", text="Entity")
        import_box.operator("witcher.cutscene_scratch_import_camera", text="Load Rig", icon='IMPORT')
        return

    rig_box = layout.box()
    cuts = _cs_iter_camera_cuts(camera_arm)
    cut_idx = _cs_current_cut_index(context, camera_arm, cuts)
    current_strip = cuts[cut_idx][1] if cut_idx >= 0 else None

    hdr = rig_box.row(align=True)
    hdr.label(text=camera_arm.name, icon='ARMATURE_DATA')
    if current_strip is not None:
        hdr.label(text=f"Cut {cut_idx + 1}/{len(cuts)}  {int(current_strip.frame_start)}–{int(current_strip.frame_end)}", icon='SEQUENCE')
    else:
        hdr.label(text="No cuts", icon='SEQUENCE')

    preview_row = rig_box.row(align=True)
    preview_row.operator("witcher.camera_setup_preview", text="Setup Preview", icon='CAMERA_DATA')
    preview_row.operator("witcher.camera_set_scene_camera", text="Set Scene Camera", icon='VIEW_CAMERA')

    key_row = rig_box.row(align=True)
    key_row.operator("witcher.camera_key_rig_from_scene_camera", text="Key Rig + DOF", icon='KEY_HLT')
    key_row.operator("witcher.camera_bake_cut_from_scene_camera", text="Bake Cut + DOF", icon='REC')

    convert_row = rig_box.row(align=True)
    convert_row.operator("witcher.camera_convert_cuts_to_blender_cameras", text="Cuts → Blender Cams", icon='CAMERA_DATA')
    convert_row.operator("witcher.cutscene_scratch_bake_selected_camera_range", text="Bake Selected Range", icon='REC')

    nav_row = rig_box.row(align=True)
    nav_row.operator("witcher.camera_cut_jump", text="", icon='TRIA_LEFT').direction = 'PREV'
    nav_row.operator("witcher.camera_cut_jump", text="Current Cut", icon='PREVIEW_RANGE').direction = 'CURRENT'
    nav_row.operator("witcher.camera_cut_jump", text="", icon='TRIA_RIGHT').direction = 'NEXT'

    edit_row = rig_box.row(align=True)
    edit_row.operator("witcher.camera_cut_split", text="Cut", icon='MOD_BOOLEAN')
    edit_row.operator("witcher.camera_cut_combine", text="Combine", icon='NLA')
    edit_row.operator("witcher.cutscene_scratch_create_camera_cut", text="", icon='SEQUENCE')

    resize_row = rig_box.row(align=True)
    resize_row.operator("witcher.camera_cut_resize", text="-5", icon='REMOVE').delta = -5
    resize_row.operator("witcher.camera_cut_resize", text="-1", icon='REMOVE').delta = -1
    resize_row.operator("witcher.camera_cut_resize", text="+1", icon='ADD').delta = 1
    resize_row.operator("witcher.camera_cut_resize", text="+5", icon='ADD').delta = 5

    marker_row = rig_box.row(align=True)
    marker_row.operator("witcher.camera_cut_sync_markers", text="Sync Markers", icon='MARKER')
    marker_row.operator("witcher.camera_cut_apply_markers", text="Apply Markers", icon='CHECKMARK')

    _draw_camera_track_controls(rig_box, camera_arm)


def _draw_event_schema_browser(layout, scene, cs_selected):
    """Collapsible schema browser listing all event types grouped ROOT / ENTRY."""
    scope_filter = "ROOT" if cs_selected else "ENTRY"

    for scope_label, scope_key, icon in [
        ("Cutscene ROOT events", "ROOT", 'SCENE_DATA'),
        ("Animation ENTRY events", "ENTRY", 'ACTION'),
    ]:
        if scope_key != scope_filter:
            continue
        sect = layout.box()
        sect.label(text=scope_label, icon=icon)
        for cls_name, base_cls, scope, own_props in _CUTSCENE_EVENT_SCHEMA:
            if scope != scope_key:
                continue
            row = sect.row(align=True)
            row.scale_y = 0.85
            label_col = row.column()
            label_col.scale_x = 1.0
            # Class name + base
            name_row = label_col.row(align=True)
            name_row.label(text=cls_name, icon=_event_type_icon(cls_name))
            base_col = name_row.row()
            base_col.enabled = False
            base_col.label(text=f"↑ {base_cls}")
            # Own props list
            if own_props:
                props_row = label_col.row()
                props_row.enabled = False
                props_row.label(text="  " + "  ·  ".join(f"{n} ({t})" for n, t in own_props[:5]))
            # Add button
            add_op = row.operator("witcher.cutscene_add_event", text="", icon='ADD')
            add_op.event_scope = scope_key
            add_op.event_class = cls_name


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

    # ── Add Event ─────────────────────────────────────────────────────────────
    layout.separator(factor=0.5)
    add_box = layout.box()
    add_hdr = add_box.row(align=True)
    add_hdr.label(text="Add Event", icon='ADD')
    add_hdr.prop(scene, "witcher_cs_show_event_schema", text="Schema", toggle=True, icon='PROPERTIES')

    # Quick-add row: two most common types
    quick_row = add_box.row(align=True)
    op_cs = quick_row.operator("witcher.cutscene_add_event", text="Cutscene (ROOT)", icon='SCENE_DATA')
    op_cs.event_scope = "ROOT"
    op_cs.source_index = -1
    op_anim = quick_row.operator("witcher.cutscene_add_event", text="Animation (ENTRY)", icon='ACTION')
    op_anim.event_scope = "ENTRY"

    # Schema browser (collapsible)
    if bool(getattr(scene, "witcher_cs_show_event_schema", False)):
        _draw_event_schema_browser(add_box, scene, cs_selected)


def _draw_cutscene_dialogs_tab(layout, scene):
    header = layout.row(align=True)
    header.label(text="CStorySceneLine Dialogs", icon='OUTLINER_OB_SPEAKER')
    if hasattr(scene, dialog_language.DIALOG_TEXT_LANGUAGE_PROP):
        header.prop(scene, dialog_language.DIALOG_TEXT_LANGUAGE_PROP, text="Text")
    if hasattr(scene, dialog_language.DIALOG_VOICE_LANGUAGE_PROP):
        header.prop(scene, dialog_language.DIALOG_VOICE_LANGUAGE_PROP, text="Voice")
    header.operator(WITCH_OT_LoadCutsceneDialogs.bl_idname, text="Import/Refresh", icon='SOUND')

    display_row = layout.row(align=True)
    display_row.prop(scene, "witcher_cutscene_show_dialog_subtitles", text="Viewport Subtitles", toggle=True, icon='FONT_DATA')
    display_row.prop(scene, "witcher_cutscene_subtitle_font_size", text="Size")

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
            detail.label(text="CStorySceneLine", icon='OUTLINER_OB_SPEAKER')
            col = detail.column(align=True)
            if sel.line_text:
                col.label(text=f"LocalizedString.text: {sel.line_text}", icon='FONT_DATA', translate=False)
            else:
                col.label(text="LocalizedString.text: <not resolved>", icon='FONT_DATA')
            col.label(text=f"voicetag: {sel.actor or '<unset>'}")
            col.label(text=f"dialogLine: {sel.line_id or '<unset>'}")
            col.label(text=f"voiceFileName: {sel.voice_file or '<unset>'}")
            col.label(text=f"soundEventName: {sel.sound_event or '<unset>'}")
            if sel.scene_path:
                col.label(text=f"source .w2scene: {sel.scene_path}", icon='FILE')
            col.label(text=f"computed frame range: {sel.start_frame}-{sel.end_frame}")
    else:
        layout.label(text="Press Import/Refresh to read CStorySceneLine fields from linked .w2scene.", icon='INFO')

class WITCH_OT_CutsceneCreateNew(Operator):
    """Set up a fresh cutscene with a default repo path, clearing any previously imported file"""
    bl_idname = "witcher.cutscene_create_new"
    bl_label = "Create New Cutscene"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        if scene is None:
            return {'CANCELLED'}
        base = "animations\\cutscenes\\blender_tools\\new_cutscene"
        existing = set()
        repo_path_lower = str(getattr(scene, "witcher_cutscene_export_repo_path", "") or "").replace("/", "\\").lower()
        if repo_path_lower:
            existing.add(repo_path_lower)
        candidate = f"{base}_01.w2cutscene"
        for i in range(1, 100):
            candidate = f"{base}_{i:02d}.w2cutscene"
            if candidate.lower() not in existing:
                break
        scene.witcher_cutscene_export_repo_path = candidate
        scene.witcher_loaded_w2cutscene_path = ""
        self.report({'INFO'}, f"New cutscene: {candidate}")
        return {'FINISHED'}

class WITCHER_PT_cutscene_panel(WITCH_PT_Base, Panel):
    bl_idname = "WITCHER_PT_cutscene_panel"
    bl_label = "Cutscene"
    bl_description = ""
    bl_options = {'DEFAULT_CLOSED'}

    def draw_header(self, context):
        self.layout.label(text="", icon='ACTION')

    def draw(self, context):
        scene = context.scene
        if scene is None:
            return

        layout = self.layout

        if hasattr(scene, "witcher_cutscene_export_repo_path"):
            layout.prop(scene, "witcher_cutscene_export_repo_path", text="Repo Path")
        action_row = layout.row(align=True)
        action_row.operator(ButtonOperatorImportW2cutscene.bl_idname, text="Import", icon='IMPORT')
        action_row.operator("witcher.export_w2_cutscene", text="Export", icon='EXPORT')
        action_row.operator(WITCH_OT_CutsceneCreateNew.bl_idname, text="New", icon='FILE_NEW')

        loaded_cutscene_path = str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or "").strip()
        if loaded_cutscene_path and not scene.witcher_cutscene_actor_items and not scene.witcher_cutscene_animation_items:
            _schedule_deferred_cutscene_state_sync(scene, loaded_cutscene_path)

        if loaded_cutscene_path:
            hdr_row = layout.row(align=True)
            cs_name = scene.witcher_loaded_cutscene_name or _get_loaded_cutscene_name(loaded_cutscene_path)
            hdr_row.label(text=cs_name, icon='ACTION')
            hdr_row.operator(WITCH_OT_ReopenCutsceneImportDialog.bl_idname, text="", icon='FILE_REFRESH')
            last_import_seconds = float(getattr(scene, "witcher_cutscene_last_import_seconds", 0.0) or 0.0)
            if last_import_seconds > 0.0:
                layout.label(text=f"Last import: {last_import_seconds:.2f}s", icon='TIME')

        prev_split = layout.use_property_split
        layout.use_property_split = False
        tab_row = layout.row(align=True)
        tab_row.scale_y = 1.2
        tab_row.prop_enum(scene, "witcher_cs_tab", 'TEMPLATE')
        tab_row.prop_enum(scene, "witcher_cs_tab", 'ACTORS')
        tab_row.prop_enum(scene, "witcher_cs_tab", 'ANIMS')
        tab_row = layout.row(align=True)
        tab_row.scale_y = 1.2
        tab_row.prop_enum(scene, "witcher_cs_tab", 'CAMERA')
        tab_row.prop_enum(scene, "witcher_cs_tab", 'EVENTS')
        tab_row.prop_enum(scene, "witcher_cs_tab", 'DIALOGS')
        layout.use_property_split = prev_split
        layout.separator(factor=0.5)

        tab = str(getattr(scene, "witcher_cs_tab", "ACTORS") or "ACTORS")
        if tab == 'TEMPLATE':
            _draw_cutscene_template_tab(layout, scene)
        elif tab == 'ACTORS':
            _draw_cutscene_actors_tab(layout, scene, context)
        elif tab == 'ANIMS':
            _draw_cutscene_anims_tab(layout, scene, context)
        elif tab == 'CAMERA':
            _draw_cutscene_camera_tab(layout, scene, context)
        elif tab == 'EVENTS':
            _draw_cutscene_events_tab(layout, scene)
        elif tab == 'DIALOGS':
            _draw_cutscene_dialogs_tab(layout, scene)


classes = [
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
    WITCH_UL_ActorEntryEventList,
    WITCH_OT_CutsceneSelectActor,
    WITCH_OT_CutsceneRemoveActor,
    WITCH_OT_CutsceneRemoveAnimation,
    WITCH_OT_CutsceneAddEvent,
    ButtonOperatorImportW2cutscene,
    WITCH_OT_CutsceneCreateNew,
    WITCH_OT_ReopenCutsceneImportDialog,
    WITCH_OT_ImportCutsceneBurnedAudio,
    WITCH_OT_RemoveCutsceneBurnedAudio,
    WITCH_OT_SetCutsceneAnimationLoaded,
    WITCH_OT_LoadCutsceneDialogs,
    WITCHER_PT_cutscene_panel,
]


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.witcher_loaded_cutscene_name = StringProperty(default="")
    bpy.types.Scene.witcher_cutscene_last_import_seconds = FloatProperty(default=0.0)
    bpy.types.Scene.witcher_cutscene_actor_items = CollectionProperty(type=CutsceneLoadedActorItem)
    bpy.types.Scene.witcher_cutscene_animation_items = CollectionProperty(type=CutsceneLoadedAnimationItem)
    bpy.types.Scene.witcher_cutscene_event_items = CollectionProperty(type=CutsceneEventItem)
    bpy.types.Scene.witcher_cutscene_event_index = IntProperty(default=0)
    bpy.types.Scene.witcher_cs_entry_event_idx = IntProperty(default=0)
    bpy.types.Scene.witcher_cs_actor_event_idx = IntProperty(default=0)
    bpy.types.Scene.witcher_cs_actor_event_filter = StringProperty(default="", options={'SKIP_SAVE'})
    bpy.types.Scene.witcher_cs_show_event_schema = BoolProperty(name="Show Event Schema", default=False)
    bpy.types.Scene.witcher_cs_tab = EnumProperty(
        name="Cutscene Tab",
        items=[
            ('TEMPLATE', 'Template', 'Cutscene template properties, burned track and export metadata'),
            ('ACTORS', 'Actors', 'Manage cutscene actors'),
            ('ANIMS', 'Animations', 'Add animations and manage cutscene animation strips'),
            ('CAMERA', 'Camera', 'Camera entity, cut creation and camera rig tools'),
            ('EVENTS', 'Events', 'Cutscene events'),
            ('DIALOGS', 'Dialogs', 'Linked scene dialog lines, audio and viewport subtitles'),
        ],
        default='ACTORS',
    )
    bpy.types.Scene.witcher_cutscene_loaded_actor_index = IntProperty(default=0)
    bpy.types.Scene.witcher_cutscene_loaded_anim_index = IntProperty(default=0)
    bpy.types.Scene.witcher_cutscene_template_fields = CollectionProperty(type=CutsceneTemplateFieldItem)
    bpy.types.Scene.witcher_cutscene_show_unset_template_fields = BoolProperty(name="Show Unset", default=False)
    bpy.types.Scene.witcher_cutscene_burned_audio_event = StringProperty(default="")
    bpy.types.Scene.witcher_cutscene_burned_audio_item_path = StringProperty(default="")
    bpy.types.Scene.witcher_cutscene_burned_audio_default_volume = FloatProperty(
        name="Burned Track Default Volume",
        default=import_cutscene.CUTSCENE_BURNED_AUDIO_DEFAULT_VOLUME,
        min=0.0,
        soft_max=2.0,
        description="Default sequencer volume for imported cutscene burned-track strips",
    )
    bpy.types.Scene.witcher_cutscene_point_tags = StringProperty(
        name="Point Tags",
        default="",
        description="Semicolon-separated TagList value for exported cutscenes",
    )
    bpy.types.Scene.witcher_cutscene_last_level_loaded = StringProperty(
        name="Last Level Loaded",
        default="",
        description="Value written to CCutsceneTemplate.lastLevelLoaded on export",
    )
    bpy.types.Scene.witcher_cutscene_used_in_files = StringProperty(
        name="Used In Files",
        default="",
        description="Semicolon-separated depot paths for CCutsceneTemplate.usedInFiles on export",
    )
    bpy.types.Scene.witcher_cutscene_export_metadata_synced = BoolProperty(default=False)
    bpy.types.Scene.witcher_cutscene_effect_items = CollectionProperty(type=CutsceneEffectItem)
    bpy.types.Scene.witcher_cutscene_dialog_items = CollectionProperty(type=CutsceneDialogItem)
    bpy.types.Scene.witcher_cutscene_dialog_index = IntProperty(default=0)
    bpy.types.Scene.witcher_cutscene_show_dialog_subtitles = BoolProperty(
        name="Viewport Subtitles",
        default=True,
        description="Show active dialog text in the 3D Viewport",
    )
    bpy.types.Scene.witcher_cutscene_subtitle_font_size = IntProperty(
        name="Subtitle Size",
        default=28,
        min=12,
        max=72,
        description="3D Viewport subtitle font size",
    )
    bpy.types.Scene.witcher_cutscene_selected_actor_obj = StringProperty(
        name="Selected Cutscene Actor",
        description="Name of the Blender object currently selected in the Actors tab",
        default="",
        options={'SKIP_SAVE'},
    )
    bpy.types.Object.witcher_cutscene_actor_type = EnumProperty(
        name="type",
        description="ECutsceneActorType for the cutscene actor",
        items=_ECutsceneActorType_ITEMS,
        get=_actor_type_get,
        set=_actor_type_set,
    )
    enable_cutscene_subtitles()


def unregister():
    disable_cutscene_subtitles()
    for prop in (
        "witcher_loaded_cutscene_name",
        "witcher_cutscene_last_import_seconds",
        "witcher_cutscene_actor_items",
        "witcher_cutscene_animation_items",
        "witcher_cutscene_event_items",
        "witcher_cutscene_event_index",
        "witcher_cs_tab",
        "witcher_cutscene_loaded_actor_index",
        "witcher_cutscene_loaded_anim_index",
        "witcher_cutscene_show_unset_template_fields",
        "witcher_cutscene_burned_audio_event",
        "witcher_cutscene_burned_audio_item_path",
        "witcher_cutscene_burned_audio_default_volume",
        "witcher_cutscene_point_tags",
        "witcher_cutscene_last_level_loaded",
        "witcher_cutscene_used_in_files",
        "witcher_cutscene_export_metadata_synced",
        "witcher_cs_entry_event_idx",
        "witcher_cs_actor_event_idx",
        "witcher_cs_actor_event_filter",
        "witcher_cs_show_event_schema",
        "witcher_cs_fade_before",
        "witcher_cs_fade_after",
        "witcher_cs_cam_blend_in",
        "witcher_cs_cam_blend_out",
        "witcher_cs_blackscreen",
        "witcher_cs_check_actors_pos",
        "witcher_cs_reverb_name",
        "witcher_cs_audio_track",
        "witcher_cs_ent_to_hide_tags",
        "witcher_cutscene_info_tab",
        "witcher_cutscene_event_scope_tab",
        "witcher_cutscene_events_tab",
        "witcher_cs_events_anim_idx",
        "witcher_cs_event_view",
        "witcher_cutscene_selected_actor_obj",
        "witcher_cutscene_template_fields",
        "witcher_cutscene_effect_items",
        "witcher_cutscene_dialog_items",
        "witcher_cutscene_dialog_index",
        "witcher_cutscene_show_dialog_subtitles",
        "witcher_cutscene_subtitle_font_size",
    ):
        if hasattr(bpy.types.Scene, prop):
            delattr(bpy.types.Scene, prop)
    if hasattr(bpy.types.Object, "witcher_cutscene_actor_type"):
        del bpy.types.Object.witcher_cutscene_actor_type
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
