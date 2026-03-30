import logging
from pathlib import Path
from .. import import_anims
#from io_import_w2l.filter_list import memory
log = logging.getLogger(__name__)
from ..CR2W.common_blender import repo_file
from ..CR2W.dc_anims import load_bin_anims_single

import csv
import os
import bpy
from bpy.types import PropertyGroup
from bpy.props import (
    CollectionProperty,
    IntProperty,
    BoolProperty,
    StringProperty,
    PointerProperty,
)
from .. import get_uncook_path
from ..ui.armature_context import get_main_armature, set_main_armature


def _resolve_main_armature(context, main_arm_obj=None):
    if main_arm_obj and main_arm_obj.type == "ARMATURE":
        try:
            set_main_armature(context.scene, main_arm_obj)
        except Exception:
            pass
        return main_arm_obj
    return get_main_armature(
        context,
        prefer_active=True,
        remember=True,
        fallback=True,
        allow_auxiliary_active=True,
    )


def is_face_animation(anim_name, fdir=""):
    anim_text = str(anim_name or "").strip().lower()
    if ":face" in anim_text:
        return True
    return "_mimic_" in str(fdir or "").strip().lower()


def _is_mimic_component_armature(obj):
    if not obj or getattr(obj, "type", None) != "ARMATURE":
        return False
    component_type = str(obj.get("witcher_type", "") or "").strip()
    if component_type == "CMimicComponent":
        return True
    object_name = str(getattr(obj, "name", "") or "")
    if "cmimiccomponent" in object_name.lower():
        return True
    mimic_name = str(obj.get("mimicFace", "") or "").strip()
    mimic_face_file = str(obj.get("mimicFaceFile", "") or "").strip()
    if mimic_face_file and mimic_name and mimic_name == object_name:
        return True
    return False


def _iter_descendant_armatures(root_obj):
    if not root_obj:
        return
    pending = list(getattr(root_obj, "children", []) or [])
    while pending:
        child = pending.pop(0)
        pending.extend(getattr(child, "children", []) or [])
        if getattr(child, "type", None) == "ARMATURE":
            yield child


def _find_named_mimic_armature(root_obj):
    if not root_obj:
        return None
    mimic_name = str(root_obj.get("mimicFace", "") or "").strip()
    if not mimic_name:
        return None
    candidate = bpy.data.objects.get(mimic_name)
    if _is_mimic_component_armature(candidate):
        return candidate
    return None


def _iter_related_scene_mimic_armatures(root_obj):
    if not root_obj:
        return
    root_actor_name = str(root_obj.get("cutscene_actor_name", "") or "").strip()
    root_entity_name = str(root_obj.get("witcher_entity_name", "") or "").strip()
    for obj in getattr(bpy.context.scene, "objects", []):
        if obj is root_obj or getattr(obj, "type", None) != "ARMATURE":
            continue
        if not _is_mimic_component_armature(obj):
            continue
        if root_actor_name and str(obj.get("cutscene_actor_name", "") or "").strip() == root_actor_name:
            yield obj
            continue
        if root_entity_name and str(obj.get("witcher_entity_name", "") or "").strip() == root_entity_name:
            yield obj


def _iter_related_scene_armatures(root_obj):
    if not root_obj:
        return
    root_name = str(getattr(root_obj, "name", "") or "").strip()
    root_actor_name = str(root_obj.get("cutscene_actor_name", "") or "").strip()
    root_entity_name = str(root_obj.get("witcher_entity_name", "") or "").strip()
    for obj in getattr(bpy.context.scene, "objects", []):
        if obj is root_obj or getattr(obj, "type", None) != "ARMATURE":
            continue
        if root_name and str(obj.get("mimicFace", "") or "").strip() == root_name:
            yield obj
            continue
        if root_actor_name and str(obj.get("cutscene_actor_name", "") or "").strip() == root_actor_name:
            yield obj
            continue
        if root_entity_name and str(obj.get("witcher_entity_name", "") or "").strip() == root_entity_name:
            yield obj


def _iter_parent_related_armatures(root_obj):
    if not root_obj:
        return
    parent_obj = getattr(root_obj, "parent", None)
    if parent_obj is None:
        return
    pending = list(getattr(parent_obj, "children", []) or [])
    while pending:
        child = pending.pop(0)
        pending.extend(getattr(child, "children", []) or [])
        if child is root_obj or getattr(child, "type", None) != "ARMATURE":
            continue
        yield child


def _unique_armatures(armatures):
    unique = []
    seen_names = set()
    for armature_obj in armatures or []:
        if not armature_obj or getattr(armature_obj, "type", None) != "ARMATURE":
            continue
        if armature_obj.name in seen_names:
            continue
        seen_names.add(armature_obj.name)
        unique.append(armature_obj)
    return unique


def _resolve_face_animation_targets(main_arm_obj):
    if _is_mimic_component_armature(main_arm_obj):
        return [main_arm_obj]
    mimic_targets = []
    named_mimic = _find_named_mimic_armature(main_arm_obj)
    if named_mimic is not None:
        mimic_targets.append(named_mimic)
    mimic_targets.extend(
        armature_obj
        for armature_obj in _iter_descendant_armatures(main_arm_obj)
        if _is_mimic_component_armature(armature_obj)
    )
    mimic_targets.extend(
        armature_obj
        for armature_obj in _iter_parent_related_armatures(main_arm_obj)
        if _is_mimic_component_armature(armature_obj)
    )
    mimic_targets.extend(_iter_related_scene_mimic_armatures(main_arm_obj))
    return _unique_armatures(mimic_targets)


def _iter_animation_buffers(animation):
    if animation is None:
        return
    anim_buffer = getattr(animation, "animBuffer", None)
    if anim_buffer is None:
        return
    parts = getattr(anim_buffer, "parts", None)
    if parts:
        for part in parts:
            if part is not None:
                yield part
        return
    yield anim_buffer


def _animation_has_float_tracks(animation):
    for anim_buffer in _iter_animation_buffers(animation):
        if len(getattr(anim_buffer, "tracks", []) or []):
            return True
    return False


def _resolve_face_track_target_armature(main_arm_obj, target_armatures):
    candidates = []
    if main_arm_obj and getattr(main_arm_obj, "type", None) == "ARMATURE":
        candidates.append(main_arm_obj)
    candidates.extend(target_armatures or [])
    candidates.extend(_iter_parent_related_armatures(main_arm_obj))
    candidates.extend(_iter_descendant_armatures(main_arm_obj))
    candidates.extend(_iter_related_scene_armatures(main_arm_obj))

    for armature_obj in _unique_armatures(candidates):
        if not _is_mimic_component_armature(armature_obj):
            return armature_obj
    return None


def _iter_owner_armatures_for_mimic(mimic_arm_obj):
    if not mimic_arm_obj or getattr(mimic_arm_obj, "type", None) != "ARMATURE":
        return
    mimic_name = str(getattr(mimic_arm_obj, "name", "") or "").strip()
    if not mimic_name:
        return

    mimic_actor_name = str(mimic_arm_obj.get("cutscene_actor_name", "") or "").strip()
    mimic_entity_name = str(mimic_arm_obj.get("witcher_entity_name", "") or "").strip()
    actor_matches = []
    entity_matches = []
    fallback_matches = []

    for obj in getattr(bpy.context.scene, "objects", []):
        if obj is mimic_arm_obj or getattr(obj, "type", None) != "ARMATURE":
            continue
        if _is_mimic_component_armature(obj):
            continue
        if str(obj.get("mimicFace", "") or "").strip() != mimic_name:
            continue
        if mimic_actor_name and str(obj.get("cutscene_actor_name", "") or "").strip() == mimic_actor_name:
            actor_matches.append(obj)
            continue
        if mimic_entity_name and str(obj.get("witcher_entity_name", "") or "").strip() == mimic_entity_name:
            entity_matches.append(obj)
            continue
        fallback_matches.append(obj)

    for owner_group in (actor_matches, entity_matches, fallback_matches):
        for owner_armature in _unique_armatures(owner_group):
            yield owner_armature


def _resolve_owner_face_target_armature(main_arm_obj):
    if not main_arm_obj or getattr(main_arm_obj, "type", None) != "ARMATURE":
        return None
    if not _is_mimic_component_armature(main_arm_obj):
        return main_arm_obj
    for owner_armature in _iter_owner_armatures_for_mimic(main_arm_obj):
        return owner_armature
    return main_arm_obj


def resolve_owner_face_animation_context(context, main_arm_obj=None):
    resolved_main_arm_obj = _resolve_main_armature(context, main_arm_obj)
    if not resolved_main_arm_obj:
        raise RuntimeError("No armature found. Select or import a rig first.")

    owner_armature = _resolve_owner_face_target_armature(resolved_main_arm_obj) or resolved_main_arm_obj
    try:
        set_main_armature(context.scene, owner_armature)
    except Exception:
        pass

    rig_path = _resolve_face_rig_path(owner_armature, [owner_armature])
    if rig_path is None and owner_armature is not resolved_main_arm_obj:
        rig_path = _resolve_face_rig_path(resolved_main_arm_obj, [resolved_main_arm_obj])
    if rig_path is None:
        log.warning("No face rig path found for '%s', will use default skeleton.", owner_armature.name)

    return resolved_main_arm_obj, owner_armature, rig_path


def ensure_face_animation_setup(context, main_arm_obj, target_armatures=None):
    track_target_armature = _resolve_face_track_target_armature(main_arm_obj, target_armatures or [])
    if track_target_armature is None:
        return False, None
    if _is_mimic_component_armature(track_target_armature):
        return False, track_target_armature

    try:
        from .ui_mimics import _ensure_face_morphs_loaded

        return bool(_ensure_face_morphs_loaded(context, track_target_armature)), track_target_armature
    except Exception:
        log.warning(
            "Failed to ensure face morph setup on '%s'.",
            getattr(track_target_armature, "name", "<unknown>"),
            exc_info=True,
        )
        return False, track_target_armature


def ensure_owner_face_animation_setup(context, main_arm_obj=None):
    _resolved_main_arm_obj, owner_armature, _rig_path = resolve_owner_face_animation_context(context, main_arm_obj)
    try:
        from .ui_mimics import _ensure_face_morphs_loaded

        return bool(_ensure_face_morphs_loaded(context, owner_armature)), owner_armature
    except Exception:
        log.warning(
            "Failed to ensure face morph setup on '%s'.",
            getattr(owner_armature, "name", "<unknown>"),
            exc_info=True,
        )
        return False, owner_armature


def _resolve_entity_rig_path(main_arm_obj):
    if not main_arm_obj or getattr(main_arm_obj, "type", None) != "ARMATURE":
        return None
    rig_settings = getattr(main_arm_obj.data, "witcherui_RigSettings", None)
    skeleton_path = str(getattr(rig_settings, "main_entity_skeleton", "") or "").strip() if rig_settings else ""
    if not skeleton_path:
        return None
    try:
        return repo_file(skeleton_path)
    except Exception:
        return None


def _resolve_face_rig_path(main_arm_obj, target_armatures):
    candidates = []
    if main_arm_obj:
        candidates.append(main_arm_obj)
    candidates.extend(target_armatures or [])
    for armature_obj in _unique_armatures(candidates):
        mimic_face_file = str(armature_obj.get("mimicFaceFile", "") or "").strip()
        if mimic_face_file:
            try:
                return repo_file(mimic_face_file)
            except Exception:
                pass
        rig_settings = getattr(armature_obj.data, "witcherui_RigSettings", None)
        skeleton_path = str(getattr(rig_settings, "main_face_skeleton", "") or "").strip() if rig_settings else ""
        if skeleton_path:
            try:
                return repo_file(skeleton_path)
            except Exception:
                pass
    return None


def resolve_animation_load_context(context, anim_name, fdir="", main_arm_obj=None):
    main_arm_obj = _resolve_main_armature(context, main_arm_obj)
    if not main_arm_obj:
        raise RuntimeError("No armature found. Select or import a rig first.")

    face_animation = is_face_animation(anim_name, fdir)
    if face_animation:
        target_armatures = _resolve_face_animation_targets(main_arm_obj)
        if not target_armatures:
            raise RuntimeError("No CMimicComponent armature found for face animation.")
        rig_path = _resolve_face_rig_path(main_arm_obj, target_armatures)
    else:
        target_armatures = [main_arm_obj]
        rig_path = _resolve_entity_rig_path(main_arm_obj)
    return main_arm_obj, target_armatures, rig_path, face_animation


_QUICK_ANIM_FILTER_CACHE = {}
_ACTIVE_SOURCE_KEY_BY_SCENE = {}
_LAST_QUICK_ANIM_SEARCH_BY_SCENE = {}
_MAX_QUICK_ANIM_CACHE_ENTRIES = 256
_POPULATING_QUICK_ANIM_LIST = False
_AUTO_LOADING_QUICK_ANIM = False
_QUICK_ANIM_DEFERRED = False
_ACTIVE_SOURCE_KEY_SENTINEL = object()  # distinguishes "never built" from key=None (show-all)


def _deferred_setup_quick_anim_list():
    global _QUICK_ANIM_DEFERRED
    _QUICK_ANIM_DEFERRED = False
    try:
        import bpy
        context = bpy.context
        scene = getattr(context, "scene", None)
        show_all = bool(getattr(scene, "witcher_quick_anim_show_all", False))
        main_arm_obj = _resolve_main_armature(context)
        SetupActor(main_arm_obj, context=context, show_all=show_all)
    except Exception:
        log.warning("Deferred quick anim list setup failed.", exc_info=True)
    return None


def _schedule_deferred_quick_anim_setup():
    global _QUICK_ANIM_DEFERRED
    if _QUICK_ANIM_DEFERRED:
        return
    _QUICK_ANIM_DEFERRED = True
    try:
        import bpy
        bpy.app.timers.register(_deferred_setup_quick_anim_list, first_interval=0.0)
    except Exception:
        _QUICK_ANIM_DEFERRED = False


def ensure_quick_anim_list_current(context):
    """Called from draw each frame. Schedules a deferred rebuild if character or show_all changed."""
    if _QUICK_ANIM_DEFERRED:
        return
    scene = getattr(context, "scene", None)
    if scene is None:
        return
    show_all = bool(getattr(scene, "witcher_quick_anim_show_all", False))
    main_arm_obj = _resolve_main_armature(context)
    current_key = _get_quick_anim_source_key(main_arm_obj, show_all)
    stored_key = _ACTIVE_SOURCE_KEY_BY_SCENE.get(_scene_key(scene), _ACTIVE_SOURCE_KEY_SENTINEL)
    if stored_key != current_key:
        _schedule_deferred_quick_anim_setup()


def _scene_key(scene):
    if scene is None:
        return 0
    try:
        return int(scene.as_pointer())
    except Exception:
        return 0


def _get_quick_anim_source_key(main_arm_obj, show_all=False):
    if show_all or not main_arm_obj or main_arm_obj.type != "ARMATURE":
        return ("__show_all__",)
    rig_settings = getattr(main_arm_obj.data, "witcherui_RigSettings", None)
    if rig_settings is None:
        return ("__show_all__",)
    anim_paths = tuple(set.path for set in rig_settings.animset_list if ":" not in set.path)
    return (
        main_arm_obj.name,
        getattr(rig_settings, "main_entity_skeleton", ""),
        getattr(rig_settings, "main_face_skeleton", ""),
        anim_paths,
    )


def _set_quick_anim_cache(cache_key, items):
    if cache_key is None:
        return
    _QUICK_ANIM_FILTER_CACHE[cache_key] = items
    if len(_QUICK_ANIM_FILTER_CACHE) > _MAX_QUICK_ANIM_CACHE_ENTRIES:
        oldest_key = next(iter(_QUICK_ANIM_FILTER_CACHE.keys()))
        _QUICK_ANIM_FILTER_CACHE.pop(oldest_key, None)


def _filtered_list_to_cache_items(filteredList):
    items = []
    for (i, item) in enumerate(filteredList):
        items.append({
            "id": str(item.id),
            "prefix": item.prefix,
            "suffix": item.suffix,
            "caption": item.caption,
            "child_count": str(item.child_count),
            "isSelected": bool(item.isSelected),
            "name": "{}{}{}".format(item.prefix, item.caption, item.suffix),
            "animLineId": str(i),
        })
    return items


def _apply_cached_items_to_scene(scene, cached_items, preferred_id=None):
    global _POPULATING_QUICK_ANIM_LIST
    if scene is None:
        return
    myAnims = scene.witcher_quick_anim_list
    old_index = int(getattr(scene, "witcher_quick_anim_list_index", 0))
    old_selected_id = None
    if len(myAnims) > 0 and 0 <= old_index < len(myAnims):
        old_selected_id = myAnims[old_index].id
    if preferred_id:
        old_selected_id = preferred_id

    _POPULATING_QUICK_ANIM_LIST = True
    try:
        myAnims.clear()
        new_index = -1
        for item_data in cached_items:
            anim = myAnims.add()
            anim.id = item_data["id"]
            anim.prefix = item_data["prefix"]
            anim.suffix = item_data["suffix"]
            anim.caption = item_data["caption"]
            anim.child_count = item_data["child_count"]
            anim.isSelected = item_data["isSelected"]
            anim.name = item_data["name"]
            anim.selfIndex = len(myAnims)-1
            anim.animLineId = item_data["animLineId"]
            if old_selected_id and anim.id == old_selected_id:
                new_index = anim.selfIndex

        if len(myAnims) == 0:
            scene.witcher_quick_anim_list_index = 0
        elif new_index >= 0:
            scene.witcher_quick_anim_list_index = new_index
        else:
            scene.witcher_quick_anim_list_index = max(0, min(old_index, len(myAnims) - 1))
    finally:
        _POPULATING_QUICK_ANIM_LIST = False


def _load_selected_quick_anim(context):
    main_arm_obj = _resolve_main_armature(context)
    scene = context.scene
    if not main_arm_obj or scene.witcher_quick_anim_list_index < 0 or not scene.witcher_quick_anim_list:
        return False

    manager = CModStoryBoardAnimationListsManager.active
    if manager is None:
        return False

    item = scene.witcher_quick_anim_list[scene.witcher_quick_anim_list_index]
    try:
        anim_id = int(item.id)
    except Exception:
        return False

    anim_name, fdir = manager.getAnimationName(anim_id)
    if not anim_name or not fdir:
        return False
    fdir_abs = repo_file(fdir)
    load_anim_into_scene(context, anim_name, fdir_abs, main_arm_obj)
    if getattr(context.scene, "witcher_auto_orient_root", True):
        try:
            from ..ui.ui_anims import apply_root_orientation
            apply_root_orientation(main_arm_obj)
        except Exception as exc:
            log.warning("Quick anim auto orient failed: %s", exc)
    return True


class AnimsResourceManager:
    resourceManager = None
    def __init__(self):

        RES_DIR = Path(__file__)
        RES_DIR = str(Path(RES_DIR).parents[1])
        filename = os.path.join(RES_DIR, "CR2W\\data\\actor_animations.csv")
        self.pathashespath = filename
        #self.HashdumpDict = {}
        reader = csv.DictReader(open(self.pathashespath), delimiter=";")
        
        self.HashdumpDict = list(reader)
        # for row in reader:
        #     self.HashdumpDict[row["file"]+";"+row["id"]] = row["id"]
            #self.HashdumpDict[row["file"]] = row["cat1"]+" "+row["cat2"]+" "+row["cat3"]+": "+row["id"]+" "+row["caption"]+row["frames"]
    @staticmethod
    def Get():
        if (AnimsResourceManager.resourceManager == None):
            AnimsResourceManager.resourceManager = AnimsResourceManager();
        return AnimsResourceManager.resourceManager;


class MyAnimListItem(bpy.types.PropertyGroup):
    id: bpy.props.StringProperty(default="")
    prefix: bpy.props.StringProperty(default="")
    suffix: bpy.props.StringProperty(default="")
    caption: bpy.props.StringProperty(default="")
    child_count: bpy.props.StringProperty(default="")
    isSelected: bpy.props.BoolProperty(default=False)

    #?parent data??
    indent: bpy.props.IntProperty(default=0)
    expanded: bpy.props.BoolProperty(default=False)
    nodeIndex : bpy.props.IntProperty(default=-1) #index into the real tree data.
    
    name : bpy.props.StringProperty(default="")
    selfIndex : bpy.props.IntProperty(default=-1)
    parentIndex : bpy.props.IntProperty(default=-1)
    childCount: bpy.props.IntProperty(default=0) #should equal myNodes[nodeIndex].childCount
    animLineId: bpy.props.StringProperty(default="0000000000")
    vertex_group: bpy.props.StringProperty(default="")



def AddCLayerGroupExample(groups, parent_collection):
    this_collection = bpy.data.collections.new(groups.name)
    this_collection['group_type'] = "LayerGroup"
    if parent_collection:
        parent_collection.children.link(this_collection)
    if groups.ChildrenGroups:
        for subgroups in groups.ChildrenGroups:
            AddCLayerGroupExample(subgroups, this_collection)
    if groups.ChildrenInfos:
        for ChildInfo in groups.ChildrenInfos:
            child_collection = bpy.data.collections.new(os.path.basename(ChildInfo.depotFilePath))
            child_collection['level_path'] = ChildInfo.depotFilePath
            child_collection['layerBuildTag'] = ChildInfo.layerBuildTag
            child_collection['group_type'] = "LayerInfo"
            this_collection.children.link(child_collection)

def createCat(cat_name, dict):
    final_list = []
    for entry in dict:
        if entry['cat1'] == cat_name:
            final_list.append(entry)
    return final_list

# def get_filtered_dict(cat_name, dict, cat_num):
#     filtered_dictionary = {}
#     for key, value in enumerate(dict):
#         if (value['cat'+str(cat_num)] == cat_name):
#             filtered_dictionary[value['cat'+str(cat_num+1)]] = get_filtered_dict()
#     return filtered_dictionary

from ..filtered_list.animations_manager import CModStoryBoardAnimationListsManager
from ..filtered_list.storyboardasset import CModStoryBoardActor

def GetAnimationInfoByName(anim_name):
    uncook_path = get_uncook_path(bpy.context)
    manager = CModStoryBoardAnimationListsManager.active
    fdir = None
    found = False
    for anim in manager._animMeta.animList:
        if anim.id == anim_name:
            fdir = anim.path # animation might not be proper
            for anim_active in manager.active.active_list._items:
                if anim_active.id == anim.slotId:
                    fdir = anim.path
                    found = True
                    break
            if found:
                break
    if fdir == None:
        log.critical('Did not find animation!')
        return (None, None)
    #(, ) = item.animLineId.split(';')
    fdir = os.path.join(uncook_path, fdir)
    return (anim_name, fdir)

def SetupActor(main_arm_obj, context=None, show_all=False):
    scene = (context.scene if context else bpy.context.scene)
    scene_id = _scene_key(scene)
    show_all = show_all or not main_arm_obj
    source_key = _get_quick_anim_source_key(main_arm_obj, show_all)

    animListsManager: CModStoryBoardAnimationListsManager = CModStoryBoardAnimationListsManager()
    actor = CModStoryBoardActor()

    if show_all:
        actor._animPaths = None  # isCompatibleAnimation returns True for all
    else:
        rig_settings = getattr(main_arm_obj.data, "witcherui_RigSettings", None)
        if rig_settings is None:
            log.warning("Armature '%s' has no rig settings; falling back to show-all.", main_arm_obj.name)
            actor._animPaths = None
            source_key = ("__show_all__",)
        else:
            animset_list = rig_settings.animset_list
            actor._animPaths = []
            for set in animset_list:
                if ":" not in set.path:
                    actor._animPaths.append(set.path)
    
    animListsManager.lazyLoad()

    #TODO list should be filtered by the list of w2anims passed into it from the entity object
    list = animListsManager.getAnimationListFor(actor)
    auto_collapse = bool(getattr(scene, "witcher_quick_anim_auto_collapse_categories", True))
    if hasattr(list, "setAutoCollapseCategories"):
        list.setAutoCollapseCategories(auto_collapse)
    _ACTIVE_SOURCE_KEY_BY_SCENE[scene_id] = source_key

    cache_key = (source_key, "", auto_collapse)
    cached_items = _QUICK_ANIM_FILTER_CACHE.get(cache_key)
    if cached_items is None:
        filteredList = list.getFilteredList()
        log.debug("matching: %d / %d", list.getMatchingItemCount(), list.getTotalCount())
        cached_items = _filtered_list_to_cache_items(filteredList)
        _set_quick_anim_cache(cache_key, cached_items)
    _apply_cached_items_to_scene(scene, cached_items)

def SetupNodeData(context):
    scene = getattr(context, "scene", None)
    show_all = bool(getattr(scene, "witcher_quick_anim_show_all", False))
    main_arm_obj = _resolve_main_armature(context)
    SetupActor(main_arm_obj, context=context, show_all=show_all)

def FilterData(context):
    scene = context.scene
    show_all = bool(getattr(scene, "witcher_quick_anim_show_all", False))
    main_arm_obj = _resolve_main_armature(context)
    if not main_arm_obj and not show_all:
        return

    source_key = _get_quick_anim_source_key(main_arm_obj, show_all)
    scene_id = _scene_key(scene)
    active_source = _ACTIVE_SOURCE_KEY_BY_SCENE.get(scene_id, _ACTIVE_SOURCE_KEY_SENTINEL)
    source_changed = active_source != source_key
    if source_changed:
        SetupActor(main_arm_obj, context=context, show_all=show_all)

    search = str(scene.witcher_quick_anim_search or "")
    last_search = _LAST_QUICK_ANIM_SEARCH_BY_SCENE.get(scene_id, "")
    search_changed = (last_search != search)

    auto_collapse = bool(getattr(scene, "witcher_quick_anim_auto_collapse_categories", True))
    list = CModStoryBoardAnimationListsManager.active_list
    if list:
        if hasattr(list, "setAutoCollapseCategories"):
            list.setAutoCollapseCategories(auto_collapse)
        # Search UX: expand all matches by default when the query changes
        # (or the actor/source changes under an active query).
        if search and (search_changed or source_changed) and hasattr(list, "setExpandAll"):
            list.setExpandAll(True)
        elif (not search) and search_changed and hasattr(list, "setExpandAll"):
            list.setExpandAll(False)
        _apply_quick_anim_wildcard_filter(list, search, preserve_selection=False)

    _LAST_QUICK_ANIM_SEARCH_BY_SCENE[scene_id] = search

    cache_key = (source_key, search, auto_collapse)
    use_cache = not (search and (search_changed or source_changed))
    cached_items = _QUICK_ANIM_FILTER_CACHE.get(cache_key) if use_cache else None
    if cached_items is not None:
        _apply_cached_items_to_scene(scene, cached_items)
        return

    if list:
        filteredList = list.getFilteredList()
        log.debug("matching: %d / %d", list.getMatchingItemCount(), list.getTotalCount())
        cached_items = _filtered_list_to_cache_items(filteredList)
        _set_quick_anim_cache(cache_key, cached_items)
        _apply_cached_items_to_scene(scene, cached_items)

def load_anim_into_scene(context, anim_name, fdir, main_arm_obj, NLA_track = 'anim_import', at_frame = 0,
                         face_target_mode="auto"):
    face_animation = is_face_animation(anim_name, fdir)
    if face_target_mode == "owner" and face_animation:
        main_arm_obj, owner_armature, rig_path = resolve_owner_face_animation_context(
            context,
            main_arm_obj=main_arm_obj,
        )
        target_armatures = [owner_armature]
    else:
        main_arm_obj, target_armatures, rig_path, face_animation = resolve_animation_load_context(
            context,
            anim_name,
            fdir=fdir,
            main_arm_obj=main_arm_obj,
        )
    effective_track = NLA_track
    if face_target_mode != "owner" and face_animation and NLA_track == 'anim_import':
        effective_track = 'mimic_import'

    result = load_bin_anims_single(
        fdir,
        anim_name,
        rigPath=rig_path,
    )
    if not result or not result.animations:
        raise RuntimeError(f"Animation '{anim_name}' was not found in {fdir}")
    animation = result.animations[0]

    actual_target_armatures = target_armatures
    if face_target_mode == "owner" and face_animation:
        face_setup_loaded, owner_armature = ensure_owner_face_animation_setup(
            context,
            main_arm_obj,
        )
        if owner_armature is not None:
            actual_target_armatures = [owner_armature]
        if not face_setup_loaded:
            target_name = getattr(owner_armature, "name", getattr(main_arm_obj, "name", "<unknown>"))
            raise RuntimeError(f"Face morphs not loaded on '{target_name}'. Ensure the entity was imported with its face component, then load face morphs before importing face animations.")
    elif face_animation and _animation_has_float_tracks(animation):
        _face_setup_loaded, track_target_armature = ensure_face_animation_setup(
            context,
            main_arm_obj,
            target_armatures,
        )
        if track_target_armature is not None:
            actual_target_armatures = [track_target_armature]
            log.info(
                "Routing face track animation '%s' to '%s' instead of mimic armature.",
                anim_name,
                track_target_armature.name,
            )
     
    #!REMOVE
    #import json
    # with open("anim_debug_example.json", "w") as file:
    #     file.write(json.dumps(animation, indent=2, default=vars, sort_keys=False))

    import_anims.import_anim(
        context,
        fdir,
        animation,
        use_NLA=True,
        NLA_track=effective_track,
        override_select=actual_target_armatures if len(actual_target_armatures) > 1 else actual_target_armatures[0],
        at_frame=at_frame,
    )
    return actual_target_armatures
    # print(fdir)
    # print(anim_name)

class MyAnimListItem_Debug(bpy.types.Operator):
    bl_idname = "witcher.myanimlist_debug"
    bl_label = "Debug"
    bl_description = "Quick animation list action"

    action: StringProperty(default="default")

    @classmethod
    def description(cls, context, properties):
        if properties.action == "reset3":
            return "Rebuild the animation list from the selected character's animation sets"
        if properties.action == "load":
            return "Load the selected animation onto the active character armature"
        return "Quick animation list action"

    def execute(self, context):
        global _QUICK_ANIM_INIT_ATTEMPTED
        uncook_path = get_uncook_path(context)
        scene = context.scene
        action = self.action
        if "load" == action:
            if not _load_selected_quick_anim(context):
                self.report({'ERROR'}, "No armature found or no quick animation selected.")
                return {'CANCELLED'}
        elif "clear_search" == action:
            log.debug("=== Clear Quick Anim Search ====")
            if context.scene.witcher_quick_anim_search:
                context.scene.witcher_quick_anim_search = ""
            else:
                FilterData(context)
        elif "reset3" == action:
            log.debug("=== Rebuild Quick Anim List ====")
            CModStoryBoardAnimationListsManager.clear_shared_cache()
            scene_id = _scene_key(context.scene)
            _ACTIVE_SOURCE_KEY_BY_SCENE.pop(scene_id, None)
            _QUICK_ANIM_FILTER_CACHE.clear()
            context.scene.witcher_quick_anim_search = ""
            SetupNodeData(context)
        elif "search" == action:
            FilterData(context)
        elif "clear" == action:
            log.debug("=== Debug Clear ====")
            bpy.context.scene.witcher_quick_anim_list.clear()
        else:
            log.warning("unknown debug action: %s", action)

        return {'FINISHED'}


def _is_category_item_id(item_id):
    return isinstance(item_id, str) and item_id.startswith("CAT")


def _toggle_category_selection(list_obj, item_id):
    if list_obj is None:
        return
    if not _is_category_item_id(item_id):
        list_obj.setSelection(item_id, True)
        return

    if hasattr(list_obj, "toggleCategory"):
        list_obj.toggleCategory(item_id)
        return

    # Fallback for older filtered-list implementation.
    list_obj.setSelection(item_id, True)


def _apply_quick_anim_wildcard_filter(list_obj, search, preserve_selection=False):
    if list_obj is None:
        return

    search_text = str(search or "")
    current_filter = ""
    if hasattr(list_obj, "getWildcardFilter"):
        try:
            current_filter = str(list_obj.getWildcardFilter() or "")
        except Exception:
            current_filter = ""

    if search_text:
        # Keep category selection stable while toggling categories under an
        # existing search filter.
        if preserve_selection and current_filter == search_text:
            return
        list_obj.setWildcardFilter(search_text)
        return

    if current_filter:
        if hasattr(list_obj, "resetWildcardFilter"):
            list_obj.resetWildcardFilter()
        else:
            list_obj.setWildcardFilter("")


def _refresh_quick_anim_view_from_list(context, list_obj, preferred_id=None):
    if context is None or list_obj is None:
        return

    auto_collapse = bool(getattr(context.scene, "witcher_quick_anim_auto_collapse_categories", True))
    search = context.scene.witcher_quick_anim_search or ""
    if hasattr(list_obj, "setAutoCollapseCategories"):
        list_obj.setAutoCollapseCategories(auto_collapse)
    _apply_quick_anim_wildcard_filter(list_obj, search, preserve_selection=True)

    filteredList = list_obj.getFilteredList()
    log.debug("matching: %d / %d", list_obj.getMatchingItemCount(), list_obj.getTotalCount())
    cached_items = _filtered_list_to_cache_items(filteredList)

    main_arm_obj = _resolve_main_armature(context)
    source_key = _get_quick_anim_source_key(main_arm_obj)
    cache_key = (source_key, search, auto_collapse)
    _set_quick_anim_cache(cache_key, cached_items)
    _apply_cached_items_to_scene(context.scene, cached_items, preferred_id=preferred_id)


class OBJECT_OT_anims_skp_folder_toggle(bpy.types.Operator):
    bl_idname = 'witcher.quick_anim_folder_toggle'
    bl_label = 'operators.FolderToggle.bl_label'
    bl_description = 'operators.FolderToggle.bl_description'
    bl_options = {'REGISTER', 'UNDO'}
    
    index: bpy.props.IntProperty(options={'HIDDEN'})
    
    @classmethod
    def poll(cls, context):
        return context.scene.witcher_quick_anim_list #context.object and context.object.data.shape_keys
    
    def execute(self, context):
        key_blocks = context.scene.witcher_quick_anim_list
        if self.index < 0 or self.index >= len(key_blocks):
            return {'CANCELLED'}

        sel_item = key_blocks[self.index]

        list = CModStoryBoardAnimationListsManager.active_list
        if list:
            if hasattr(list, "setAutoCollapseCategories"):
                list.setAutoCollapseCategories(bool(getattr(context.scene, "witcher_quick_anim_auto_collapse_categories", True)))
            _toggle_category_selection(list, sel_item.id)
            _refresh_quick_anim_view_from_list(context, list, preferred_id=sel_item.id)
        return {'FINISHED'}



class OBJECT_OT_anims_category_bulk(bpy.types.Operator):
    bl_idname = 'witcher.quick_anim_category_bulk'
    bl_label = 'Category Bulk'
    bl_description = 'Expand or collapse all quick animation categories'
    bl_options = {'REGISTER', 'UNDO'}

    action: StringProperty(default="expand_all")

    @classmethod
    def poll(cls, context):
        return bool(context.scene.witcher_quick_anim_list)

    def execute(self, context):
        list_obj = CModStoryBoardAnimationListsManager.active_list
        if list_obj is None:
            return {'CANCELLED'}

        auto_collapse = bool(getattr(context.scene, "witcher_quick_anim_auto_collapse_categories", True))
        if hasattr(list_obj, "setAutoCollapseCategories"):
            list_obj.setAutoCollapseCategories(auto_collapse)

        if self.action == "expand_all":
            if hasattr(list_obj, "setExpandAll"):
                list_obj.setExpandAll(True)
        elif self.action == "collapse_all":
            if hasattr(list_obj, "setExpandAll"):
                list_obj.setExpandAll(False)
            if hasattr(list_obj, "clearOpenedCategories"):
                list_obj.clearOpenedCategories()
            if hasattr(list_obj, "_selectedCat1"):
                list_obj._selectedCat1 = ""
            if hasattr(list_obj, "_selectedCat2"):
                list_obj._selectedCat2 = ""
            if hasattr(list_obj, "_selectedCat3"):
                list_obj._selectedCat3 = ""
        else:
            return {'CANCELLED'}

        preferred_id = None
        idx = int(getattr(context.scene, "witcher_quick_anim_list_index", -1))
        if 0 <= idx < len(context.scene.witcher_quick_anim_list):
            preferred_id = context.scene.witcher_quick_anim_list[idx].id
        _refresh_quick_anim_view_from_list(context, list_obj, preferred_id=preferred_id)
        return {'FINISHED'}


class MYANIMLISTITEM_UL_basic(bpy.types.UIList):
    animListsManager: CModStoryBoardAnimationListsManager = CModStoryBoardAnimationListsManager()

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index=0, flt_flag=0):
        
        
        frame = layout.row(align=True)
        if item.id.startswith('CAT'):
            op = frame.operator(
                operator='witcher.quick_anim_folder_toggle',
                text="",
                icon= 'TRIA_RIGHT' if "+" in item.prefix else "TRIA_DOWN", #'TRIA_DOWN', 'TRIA_RIGHT'#core.folder.get_active_icon(item),
                emboss=False)

            op.index = index
            text_row = frame.row(align=True)
            text_row.alignment = 'LEFT'
            op = text_row.operator(
                operator='witcher.quick_anim_folder_toggle',
                text=item.name,
                emboss=False,
                icon="NONE")
            op.index = index
        else:
            frame.prop(
                data=item,
                property='name',
                text="",
                emboss=False,
                icon="NONE")#core.preferences.shape_key_icon)
    def filter_items(self, context, data, propname):
        scene = context.scene
        return ([],[])
            

from ..ui.ui_utils import WITCH_PT_Base
class SCENE_PT_myanimlist(WITCH_PT_Base, bpy.types.Panel):
    bl_parent_id = "WITCHER_PT_animset_panel"

    bl_label = "Quick Animation Browser"
    bl_idname = "SCENE_PT_myanimlist"
    bl_options = {'DEFAULT_CLOSED'}

    def draw_header(self, context):
        self.layout.label(text="", icon='PRESET')

    @classmethod
    def poll(cls, context):
        # Quick animation browser is now embedded directly in the Animation panel.
        return False

    def draw(self, context):
        scn = context.scene
        layout = self.layout
        layout.use_property_decorate = False

        info_box = layout.box()
        info_box.label(text="Browse common game clips after selecting a character.", icon='INFO')
        info_box.label(text="Loaded clips use the same animation workflow above.")

        search_box = layout.box()
        row = search_box.row(align=True)
        row.prop(context.scene, "witcher_quick_anim_search")
        row.operator(MyAnimListItem_Debug.bl_idname, text="", icon='VIEWZOOM').action = "search"
        row.prop(context.scene, "witcher_quick_anim_load_on_select", text="Load on Select")
        if hasattr(context.scene, "witcher_auto_orient_root"):
            row = search_box.row()
            row.prop(context.scene, "witcher_auto_orient_root", text="Auto Orient Root")
        row = search_box.row()
        row.prop(context.scene, "witcher_quick_anim_auto_collapse_categories", text="Auto Collapse Categories")
        row = search_box.row(align=True)
        row.operator("witcher.quick_anim_category_bulk", text="Expand All").action = "expand_all"
        row.operator("witcher.quick_anim_category_bulk", text="Collapse All").action = "collapse_all"

        list_box = layout.box()
        row = list_box.row()
        row.template_list(
            listtype_name='MYANIMLISTITEM_UL_basic',#'MYANIMLISTITEM_UL_basic',
            dataptr=bpy.context.scene,
            propname='witcher_quick_anim_list',
            active_dataptr=bpy.context.scene,
            active_propname='witcher_quick_anim_list_index',
            list_id='W3_UI_ANIMATION_LIST',
            rows=8)
        grid = list_box.grid_flow(columns=2)
        
        grid.operator(MyAnimListItem_Debug.bl_idname, text="Reset").action = "reset3"
        #grid.operator(MyAnimListItem_Debug.bl_idname, text="Clear").action = "clear"
        grid.operator(MyAnimListItem_Debug.bl_idname, text="Load").action = "load"


classes = (
        MyAnimListItem,
        MyAnimListItem_Debug,
        OBJECT_OT_anims_skp_folder_toggle,
        OBJECT_OT_anims_category_bulk,
        MYANIMLISTITEM_UL_basic,
        SCENE_PT_myanimlist)

def update_filter(self, context):
    #print(self.rna_type.identifier)
    if context is None or getattr(context, "scene", None) is None:
        return
    FilterData(context)


def on_auto_collapse_categories_changed(self, context):
    _QUICK_ANIM_FILTER_CACHE.clear()
    list_obj = CModStoryBoardAnimationListsManager.active_list
    if list_obj and hasattr(list_obj, "setAutoCollapseCategories"):
        list_obj.setAutoCollapseCategories(bool(getattr(context.scene, "witcher_quick_anim_auto_collapse_categories", True)))
    FilterData(context)


def on_quick_anim_list_index_changed(self, context):
    global _AUTO_LOADING_QUICK_ANIM
    if _AUTO_LOADING_QUICK_ANIM or _POPULATING_QUICK_ANIM_LIST:
        return
    scene = context.scene
    if scene.witcher_quick_anim_list_index < 0 or not scene.witcher_quick_anim_list:
        return
    if scene.witcher_quick_anim_list_index >= len(scene.witcher_quick_anim_list):
        return

    selected_item = scene.witcher_quick_anim_list[scene.witcher_quick_anim_list_index]
    selected_id = str(getattr(selected_item, "id", ""))
    if _is_category_item_id(selected_id):
        list_obj = CModStoryBoardAnimationListsManager.active_list
        if list_obj:
            if hasattr(list_obj, "setAutoCollapseCategories"):
                list_obj.setAutoCollapseCategories(
                    bool(getattr(scene, "witcher_quick_anim_auto_collapse_categories", True))
                )
            _toggle_category_selection(list_obj, selected_id)
            _refresh_quick_anim_view_from_list(context, list_obj, preferred_id=selected_id)
        return

    if not getattr(scene, "witcher_quick_anim_load_on_select", False):
        return
    try:
        _AUTO_LOADING_QUICK_ANIM = True
        _load_selected_quick_anim(context)
    except Exception as exc:
        log.error("Quick anim load-on-select failed: %s", exc)
    finally:
        _AUTO_LOADING_QUICK_ANIM = False

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    if not hasattr(bpy.types.Scene, "witcher_quick_anim_list"):
        bpy.types.Scene.witcher_quick_anim_list = bpy.props.CollectionProperty(type=MyAnimListItem)
    if not hasattr(bpy.types.Scene, "witcher_quick_anim_list_index"):
        bpy.types.Scene.witcher_quick_anim_list_index = IntProperty(
            update=on_quick_anim_list_index_changed
        )
    if not hasattr(bpy.types.Scene, "witcher_quick_anim_load_on_select"):
        bpy.types.Scene.witcher_quick_anim_load_on_select = BoolProperty(
            name="Load on Select",
            description="Automatically load animation when selecting it in the quick list",
            default=True,
        )
    if not hasattr(bpy.types.Scene, "witcher_quick_anim_auto_collapse_categories"):
        bpy.types.Scene.witcher_quick_anim_auto_collapse_categories = BoolProperty(
            name="Auto Collapse Categories",
            description="When enabled, opening one category collapses others. When disabled, categories can stay open together.",
            default=True,
            update=on_auto_collapse_categories_changed,
        )
    # bpy.types.Scene.myAnimList_pointer = PointerProperty(type=bpy.types.UIList
    #                                                      ,name = "Main Anim List")
    if not hasattr(bpy.types.Scene, "witcher_quick_anim_search"):
        bpy.types.Scene.witcher_quick_anim_search = StringProperty(
                                                name="",
                                                description="Search Animations",
                                                default="",
                                                update=update_filter)
    if not hasattr(bpy.types.Scene, "witcher_quick_anim_show_all"):
        bpy.types.Scene.witcher_quick_anim_show_all = BoolProperty(
            name="Show All Animations",
            description="Show all game animations regardless of compatibility with the current character",
            default=False,
            update=lambda self, ctx: _schedule_deferred_quick_anim_setup(),
        )

def unregister():
    _QUICK_ANIM_FILTER_CACHE.clear()
    _ACTIVE_SOURCE_KEY_BY_SCENE.clear()
    _LAST_QUICK_ANIM_SEARCH_BY_SCENE.clear()
    if hasattr(bpy.types.Scene, "witcher_quick_anim_auto_collapse_categories"):
        del bpy.types.Scene.witcher_quick_anim_auto_collapse_categories
    if hasattr(bpy.types.Scene, "witcher_quick_anim_load_on_select"):
        del bpy.types.Scene.witcher_quick_anim_load_on_select
    if hasattr(bpy.types.Scene, "witcher_quick_anim_list_index"):
        del bpy.types.Scene.witcher_quick_anim_list_index
    if hasattr(bpy.types.Scene, "witcher_quick_anim_list"):
        del bpy.types.Scene.witcher_quick_anim_list
    if hasattr(bpy.types.Scene, "witcher_quick_anim_search"):
        del bpy.types.Scene.witcher_quick_anim_search
    if hasattr(bpy.types.Scene, "witcher_quick_anim_show_all"):
        del bpy.types.Scene.witcher_quick_anim_show_all
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()

