
import logging
import os
from pathlib import Path
log = logging.getLogger(__name__)

from ..importers import import_anims
from ..importers import import_cutscene
from ..importers import import_scene

import bpy
from bpy.types import Panel, Operator, UIList, PropertyGroup
from bpy.props import IntProperty, StringProperty, CollectionProperty, FloatProperty, BoolProperty
from bpy_extras.io_utils import ImportHelper

from .. import fbx_util
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
    if not (lowered.endswith(".w2cutscene") or lowered.endswith(".json")):
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
        _cutscene, actor_items, animation_items = import_cutscene.collect_cutscene_preview(filepath)
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

    operator.cutscene_preview_status = (
        f"{len(actor_items)} actor(s), {len(animation_items)} animation(s) found"
    )
    return True

def _clear_loaded_cutscene_state(scene):
    scene.witcher_cutscene_actor_items.clear()
    scene.witcher_cutscene_animation_items.clear()
    scene.witcher_loaded_cutscene_name = ""
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

    _cutscene, actor_items, animation_items = import_cutscene.collect_cutscene_preview(filepath)

    loaded_actor_object_names = dict(getattr(cutscene_data, "loaded_actor_object_names_by_index", {}) or {})
    loaded_actor_imported_flags = dict(getattr(cutscene_data, "loaded_actor_imported_flags_by_index", {}) or {})
    loaded_actor_guid_by_index = dict(getattr(cutscene_data, "loaded_actor_guid_by_index", {}) or {})
    applied_animation_indices = {
        int(idx)
        for idx in (getattr(cutscene_data, "applied_animation_indices", []) or [])
    }

    scene.witcher_cutscene_actor_items.clear()
    scene.witcher_cutscene_animation_items.clear()
    scene.witcher_loaded_cutscene_name = _get_loaded_cutscene_name(filepath)
    scene.witcher_loaded_w2cutscene_path = filepath

    for actor_data in actor_items:
        source_index = int(actor_data["source_index"])
        state = dict(old_actor_state.get(source_index, {}))
        item = scene.witcher_cutscene_actor_items.add()
        item.source_index = source_index
        item.label = str(actor_data["label"])
        item.actor_name = str(actor_data["actor_name"])
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

    filter_glob: StringProperty(default='*.w2cutscene;*.json', options={'HIDDEN'})
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
        if not (lowered.endswith(".w2cutscene") or lowered.endswith(".json")):
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
        _sync_loaded_cutscene_state(context.scene, self.filepath, cutscene_data=cutscene_data)
        self.report(
            {'INFO'},
            (
                f"Imported {len(selected_actor_indices)} actor(s) and auto-loaded "
                f"{auto_loaded_count}/{len(selected_animation_indices)} animation(s)."
                if self.auto_apply_animations
                else f"Imported {len(selected_actor_indices)} actor(s) and listed {len(selected_animation_indices)} animation(s) from cutscene."
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

from .. import get_W3_VOICE_PATH
from ..ui.ui_utils import WITCH_PT_Base

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
        """
        """
        object = context.scene
        if object == None:
            return


        row = self.layout.row()
        row.operator(ButtonOperatorImportW2cutscene.bl_idname, text="Import CS (.w2cutscene)", icon='SPHERE')

        loaded_cutscene_path = str(getattr(object, "witcher_loaded_w2cutscene_path", "") or "").strip()
        if loaded_cutscene_path and not object.witcher_cutscene_actor_items and not object.witcher_cutscene_animation_items:
            _schedule_deferred_cutscene_state_sync(object, loaded_cutscene_path)
        if loaded_cutscene_path:
            cutscene_box = self.layout.box()
            header = cutscene_box.row(align=True)
            header.label(
                text=f"Loaded Cutscene: {object.witcher_loaded_cutscene_name or _get_loaded_cutscene_name(loaded_cutscene_path)}",
                icon='ACTION',
            )
            header.operator(WITCH_OT_ReopenCutsceneImportDialog.bl_idname, text="Import Dialog", icon='FILE_REFRESH')
            cutscene_box.label(text=loaded_cutscene_path)

            actor_box = cutscene_box.box()
            actor_box.label(text="Actors", icon='OUTLINER_OB_ARMATURE')
            actor_count = 0
            loaded_actor_count = 0
            for actor_entry in object.witcher_cutscene_actor_items:
                actor_state = _get_cutscene_actor_display_state(actor_entry)
                actor_count += 1
                if actor_state["is_loaded"]:
                    loaded_actor_count += 1
                row = actor_box.row(align=True)
                icon = 'CHECKMARK' if actor_state["is_loaded"] else 'RADIOBUT_OFF'
                label = actor_entry.label or actor_entry.actor_name or f"Actor {actor_entry.source_index + 1}"
                if actor_entry.appearance_name:
                    label = f"{label} [{actor_entry.appearance_name}]"
                row.label(text=label, icon=icon)
                if actor_state["is_loaded"] and not actor_state["imported_by_cutscene"]:
                    row.label(text="IN SCENE", icon='LINKED')
                if actor_state["is_loaded"]:
                    op = row.operator(WITCH_OT_SetCutsceneActorLoaded.bl_idname, text="", icon='X')
                    op.source_index = actor_entry.source_index
                    op.load = False
                else:
                    op = row.operator(WITCH_OT_SetCutsceneActorLoaded.bl_idname, text="", icon='IMPORT')
                    op.source_index = actor_entry.source_index
                    op.load = True
            if actor_count == 0:
                actor_box.label(
                    text="Refreshing cutscene actor state..." if loaded_cutscene_path else "No actors found in cutscene.",
                    icon='INFO',
                )
            else:
                actor_box.label(text=f"Loaded {loaded_actor_count}/{actor_count} actor(s)")

            anim_box = cutscene_box.box()
            anim_box.label(text="Animations", icon='ACTION')
            animation_count = 0
            loaded_animation_count = 0
            for animation_entry in object.witcher_cutscene_animation_items:
                animation_state = _get_cutscene_animation_display_state(object, animation_entry)
                animation_count += 1
                if animation_state["is_loaded"]:
                    loaded_animation_count += 1
                row = anim_box.row(align=True)
                icon = 'CHECKMARK' if animation_state["is_loaded"] else 'RADIOBUT_OFF'
                row.label(text=_get_cutscene_animation_label(animation_entry), icon=icon)
                if animation_entry.component_name:
                    row.label(text=animation_entry.component_name, icon='BONE_DATA')
                if animation_state["is_loaded"]:
                    op = row.operator(WITCH_OT_SetCutsceneAnimationLoaded.bl_idname, text="", icon='X')
                    op.source_index = animation_entry.source_index
                    op.load = False
                else:
                    op = row.operator(WITCH_OT_SetCutsceneAnimationLoaded.bl_idname, text="", icon='IMPORT')
                    op.source_index = animation_entry.source_index
                    op.load = True
            if animation_count == 0:
                anim_box.label(
                    text="Refreshing cutscene animation state..." if loaded_cutscene_path else "No animations found in cutscene.",
                    icon='INFO',
                )
            else:
                anim_box.label(text=f"Loaded {loaded_animation_count}/{animation_count} animation(s)")


        row = self.layout.row()
        row.operator(ButtonOperatorImportW2scene.bl_idname, text="Import Scene (.w2scene)", icon='SPHERE')
        
        
        object = context.scene
        if object == None:
            return

        box = self.layout.box()
        row = box.row()
        col = row.column(align=True)
        col.template_list("WITCHER_SECTIONS_UL_List", "", object, "witcher_sections", object, "witcher_sections_index")
        col = row.column()
        box.operator(Witcher_OT_load_section.bl_idname, text="Load Section")

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
    WITCH_UL_CutsceneActorPreview,
    WITCH_UL_CutsceneAnimationPreview,
    ButtonOperatorImportW2cutscene,
    WITCH_OT_ReopenCutsceneImportDialog,
    WITCH_OT_SetCutsceneActorLoaded,
    WITCH_OT_SetCutsceneAnimationLoaded,
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
    bpy.types.Scene.witcher_cutscene_actor_items = bpy.props.CollectionProperty(type=CutsceneLoadedActorItem)
    bpy.types.Scene.witcher_cutscene_animation_items = bpy.props.CollectionProperty(type=CutsceneLoadedAnimationItem)

def unregister():
    if hasattr(bpy.types.Scene, "witcher_sections"):
        del bpy.types.Scene.witcher_sections
    if hasattr(bpy.types.Scene, "witcher_sections_index"):
        del bpy.types.Scene.witcher_sections_index
    if hasattr(bpy.types.Scene, "witcher_sections_filepath"):
        del bpy.types.Scene.witcher_sections_filepath
    if hasattr(bpy.types.Scene, "witcher_loaded_cutscene_name"):
        del bpy.types.Scene.witcher_loaded_cutscene_name
    if hasattr(bpy.types.Scene, "witcher_cutscene_actor_items"):
        del bpy.types.Scene.witcher_cutscene_actor_items
    if hasattr(bpy.types.Scene, "witcher_cutscene_animation_items"):
        del bpy.types.Scene.witcher_cutscene_animation_items
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
