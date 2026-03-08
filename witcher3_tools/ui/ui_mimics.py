import logging
from pathlib import Path
from ..CR2W.dc_anims import load_bin_anims_single
from ..CR2W.common_blender import repo_file
from ..importers import import_anims
from ..ui.armature_context import get_main_armature

log = logging.getLogger(__name__)

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


_MIMIC_NODE_CACHE = None
_POPULATING_MIMIC_LIST = False
_AUTO_LOADING_MIMIC = False
_MIMIC_REFRESH_DEFERRED = False
MIMIC_NODES_PROP = "witcher_mimic_nodes"
MIMIC_LIST_PROP = "witcher_mimic_list"
MIMIC_LIST_INDEX_PROP = "witcher_mimic_list_index"
MIMIC_AUTO_LOAD_PROP = "witcher_quick_mimic_load_on_select"
MIMIC_SEARCH_PROP = "witcher_quick_mimic_search"
MIMIC_AUTO_COLLAPSE_PROP = "witcher_quick_mimic_auto_collapse_categories"
_MIMIC_UNCATEGORIZED_LABEL = "Uncategorized"


_MIMIC_BADGE_ABBREVIATIONS = {
    "geralt": "GER",
    "yennefer": "YEN",
    "ciri": "CIR",
    "syanna": "SYN",
    "uma": "UMA",
    "godling": "GDL",
    "weavess": "WEA",
    "werewolf": "WWF",
    "anna henrietta": "ANN",
    "man": "M",
    "woman": "W",
    "child": "CH",
    "monster": "MON",
    "layers": "LAY",
}


def _resolve_main_armature(context):
    return get_main_armature(context, prefer_active=True, remember=True, fallback=True)


def _resolve_scene(context=None):
    if context is not None:
        scene = getattr(context, "scene", None)
        if scene is not None:
            return scene
    return getattr(bpy.context, "scene", None)


def _scene_has_mimic_props(scene):
    return bool(
        scene
        and hasattr(scene, MIMIC_NODES_PROP)
        and hasattr(scene, MIMIC_LIST_PROP)
        and hasattr(scene, MIMIC_LIST_INDEX_PROP)
    )


def _is_id_write_context_error(exc):
    return "Writing to ID classes in this context is not allowed" in str(exc)


def _deferred_refresh_mimic_list():
    global _MIMIC_REFRESH_DEFERRED
    _MIMIC_REFRESH_DEFERRED = False
    try:
        RefreshMimicList()
    except Exception:
        log.warning("Deferred quick mimic refresh failed.", exc_info=True)
    return None


def _schedule_deferred_mimic_refresh():
    global _MIMIC_REFRESH_DEFERRED
    if _MIMIC_REFRESH_DEFERRED:
        return
    _MIMIC_REFRESH_DEFERRED = True
    try:
        bpy.app.timers.register(_deferred_refresh_mimic_list, first_interval=0.0)
    except Exception:
        _MIMIC_REFRESH_DEFERRED = False
        log.warning("Unable to register deferred quick mimic refresh timer.", exc_info=True)


def _has_face_morphs_loaded(armature_obj):
    if not armature_obj or armature_obj.type != 'ARMATURE':
        return False
    if not armature_obj.pose:
        return False
    control_bone = armature_obj.pose.bones.get("w3_face_poses")
    if control_bone is None:
        return False

    # Mimic track application depends on custom properties on this control bone.
    try:
        for key in control_bone.keys():
            if key != "_RNA_UI":
                return True
    except Exception:
        pass

    rig_settings = getattr(armature_obj.data, "witcherui_RigSettings", None)
    if rig_settings:
        try:
            for morph in rig_settings.witcher_morphs_list:
                if morph.type in (4, 5):
                    return True
        except Exception:
            pass
    return False


def _get_mimic_rig_path(armature_obj):
    if not armature_obj or armature_obj.type != 'ARMATURE':
        return None

    # Mimic animation tracks are defined against the face rig, not the body rig.
    mimic_face_file = (armature_obj.get("mimicFaceFile", "") or "").strip()
    if mimic_face_file:
        try:
            return repo_file(mimic_face_file)
        except Exception:
            pass

    rig_settings = getattr(armature_obj.data, "witcherui_RigSettings", None)
    if rig_settings is None:
        return None

    skeleton_path = (getattr(rig_settings, "main_face_skeleton", "") or "").strip()
    if skeleton_path:
        try:
            return repo_file(skeleton_path)
        except Exception:
            pass

    entity_skeleton = (getattr(rig_settings, "main_entity_skeleton", "") or "").strip()
    if entity_skeleton:
        log.warning(
            "Mimic face rig path is missing on '%s'; refusing to use entity skeleton for mimic decoding.",
            armature_obj.name,
        )
    return None


def _ensure_face_morphs_loaded(context, armature_obj):
    if not armature_obj or armature_obj.type != 'ARMATURE':
        return False
    if _has_face_morphs_loaded(armature_obj):
        return True
    if 'mimicFaceFile' not in armature_obj or 'mimicFace' not in armature_obj:
        return False

    view_layer = context.view_layer
    prev_active = view_layer.objects.active
    prev_selected = [obj for obj in context.selected_objects]
    prev_mode = "OBJECT"
    if prev_active:
        try:
            prev_mode = prev_active.mode
        except Exception:
            prev_mode = "OBJECT"
    try:
        if prev_active and prev_mode != "OBJECT":
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
            except Exception:
                pass

        bpy.ops.object.select_all(action='DESELECT')
        armature_obj.select_set(True)
        view_layer.objects.active = armature_obj
        result = bpy.ops.witcher.load_face_morphs()
        return ('FINISHED' in result) and _has_face_morphs_loaded(armature_obj)
    except Exception as exc:
        log.warning("Failed to auto-load face morphs: %s", exc)
        return False
    finally:
        try:
            bpy.ops.object.select_all(action='DESELECT')
            for obj in prev_selected:
                if obj and obj.name in bpy.data.objects:
                    obj.select_set(True)
            if prev_active and prev_active.name in bpy.data.objects:
                view_layer.objects.active = prev_active
                if prev_mode != "OBJECT":
                    try:
                        bpy.ops.object.mode_set(mode=prev_mode)
                    except Exception:
                        pass
        except Exception:
            pass


def _build_mimic_node_cache():
    try:
        mimic_list = MimicsResourceManager.Get()
    except Exception:
        log.warning("Failed to load mimic resource manager.", exc_info=True)
        return []
    cache_items = []
    for (mimic_line_id, item_name) in mimic_list.HashdumpDict.items():
        meta = mimic_list.MetaByKey.get(mimic_line_id, {})
        cache_items.append({
            "name": "{}".format(item_name),
            "mimicLineId": str(mimic_line_id),
            "filePath": str(meta.get("file", "")),
            "cat1": str(meta.get("cat1", "")),
            "cat2": str(meta.get("cat2", "")),
            "cat3": str(meta.get("cat3", "")),
            "caption": str(meta.get("caption", "")),
            "frames": int(meta.get("frames", 0) or 0),
            "hint": _build_mimic_hint_label(meta, str(item_name)),
        })
    cache_items.sort(key=_mimic_sort_key)
    return cache_items


def _mimic_sort_key(item_data):
    return (
        str(item_data.get("cat1", "") or "").strip().lower(),
        str(item_data.get("cat2", "") or "").strip().lower(),
        str(item_data.get("cat3", "") or "").strip().lower(),
        str(item_data.get("name", "") or "").strip().lower(),
        str(item_data.get("mimicLineId", "") or "").strip().lower(),
    )


def _mimic_search_blob(item_data):
    parts = (
        item_data.get("name", ""),
        item_data.get("caption", ""),
        item_data.get("mimicLineId", ""),
        item_data.get("filePath", ""),
        item_data.get("cat1", ""),
        item_data.get("cat2", ""),
        item_data.get("cat3", ""),
    )
    return " ".join(str(p or "").strip().lower() for p in parts if p)


def _matches_mimic_search(item_data, search_text):
    query = str(search_text or "").strip().lower()
    if not query:
        return True
    blob = _mimic_search_blob(item_data)
    tokens = [tok for tok in query.split() if tok]
    if not tokens:
        return True
    return all(tok in blob for tok in tokens)


def _iter_mimic_category_path(item_data):
    raw_values = (
        item_data.get("cat1", ""),
        item_data.get("cat2", ""),
        item_data.get("cat3", ""),
    )
    path = []
    for value in raw_values:
        cleaned = str(value or "").strip()
        if not cleaned:
            continue
        if not path or path[-1].lower() != cleaned.lower():
            path.append(cleaned)
    if not path:
        path.append(_MIMIC_UNCATEGORIZED_LABEL)
    return path


def _build_filtered_mimic_nodes(search_text):
    global _MIMIC_NODE_CACHE
    if _MIMIC_NODE_CACHE is None:
        _MIMIC_NODE_CACHE = _build_mimic_node_cache()

    filtered_items = [item for item in _MIMIC_NODE_CACHE if _matches_mimic_search(item, search_text)]
    filtered_items.sort(key=_mimic_sort_key)

    nodes = []
    category_lookup = {}
    for item_data in filtered_items:
        parent_index = -1
        path_parts = []
        for label in _iter_mimic_category_path(item_data):
            path_parts.append(label)
            category_key = "|".join(part.lower() for part in path_parts)
            lookup_key = (parent_index, category_key)
            existing_index = category_lookup.get(lookup_key)
            if existing_index is None:
                existing_index = len(nodes)
                category_lookup[lookup_key] = existing_index
                nodes.append({
                    "name": label,
                    "selfIndex": existing_index,
                    "parentIndex": parent_index,
                    "childCount": 0,
                    "mimicLineId": "",
                    "filePath": "",
                    "cat1": "",
                    "cat2": "",
                    "cat3": "",
                    "caption": "",
                    "frames": 0,
                    "hint": "",
                    "isCategory": True,
                    "categoryKey": category_key,
                })
            parent_index = existing_index

        nodes.append({
            "name": item_data.get("name", ""),
            "selfIndex": len(nodes),
            "parentIndex": parent_index,
            "childCount": 0,
            "mimicLineId": item_data.get("mimicLineId", ""),
            "filePath": item_data.get("filePath", ""),
            "cat1": item_data.get("cat1", ""),
            "cat2": item_data.get("cat2", ""),
            "cat3": item_data.get("cat3", ""),
            "caption": item_data.get("caption", ""),
            "frames": int(item_data.get("frames", 0) or 0),
            "hint": item_data.get("hint", ""),
            "isCategory": False,
            "categoryKey": "",
        })

    for node in nodes:
        parent_index = int(node.get("parentIndex", -1))
        if 0 <= parent_index < len(nodes):
            nodes[parent_index]["childCount"] = int(nodes[parent_index].get("childCount", 0)) + 1
    return nodes


def _abbrev_mimic_badge(text):
    value = (text or "").strip().lower()
    if not value:
        return ""
    return _MIMIC_BADGE_ABBREVIATIONS.get(value, value[:3].upper())


def _build_mimic_hint_label(meta, item_name):
    cat1 = (meta.get("cat1", "") or "").strip()
    cat2 = (meta.get("cat2", "") or "").strip()
    cat3 = (meta.get("cat3", "") or "").strip()
    name = (item_name or "").strip()
    caption = (meta.get("caption", "") or "").strip()

    badges = []
    # Primary audience/character hint
    if cat2 and cat2.lower() != "layers":
        badges.append(_abbrev_mimic_badge(cat2))
    elif cat1:
        badges.append(_abbrev_mimic_badge(cat1))

    # Layered mimic sets are often different from direct clips.
    if cat3.lower() == "layers" or cat2.lower() == "layers":
        badges.append("LAY")

    # Dialogue lipsync clips are often less reusable as expressions.
    lower_text = f"{name} {caption}".lower()
    if "lipsync" in lower_text:
        badges.append("LIP")

    # De-duplicate while preserving order.
    deduped = []
    for badge in badges:
        if badge and badge not in deduped:
            deduped.append(badge)
    return " ".join(deduped[:3])


def _get_target_mimic_match_text(context):
    armature_obj = _resolve_main_armature(context)
    if not armature_obj or armature_obj.type != 'ARMATURE':
        return ""

    values = [armature_obj.name]
    try:
        values.append(str(armature_obj.get("mimicFace", "") or ""))
        values.append(str(armature_obj.get("mimicFaceFile", "") or ""))
    except Exception:
        pass

    rig_settings = getattr(armature_obj.data, "witcherui_RigSettings", None)
    if rig_settings:
        values.append(str(getattr(rig_settings, "main_face_skeleton", "") or ""))
        values.append(str(getattr(rig_settings, "repo_path", "") or ""))

    return " | ".join(v for v in values if v).lower()


def _get_mimic_match_icon(context, item):
    # Only show a positive exact-match hint to avoid false negative noise.
    cat2 = (getattr(item, "cat2", "") or "").strip().lower()
    if not cat2 or cat2 == "layers":
        return ""
    target_text = _get_target_mimic_match_text(context)
    if not target_text:
        return ""
    return "CHECKMARK" if cat2 in target_text else ""


def _load_selected_mimic(context):
    scene = context.scene
    mimic_list = getattr(scene, MIMIC_LIST_PROP, None)
    item_index = int(getattr(scene, MIMIC_LIST_INDEX_PROP, -1))
    if item_index < 0 or mimic_list is None or item_index >= len(mimic_list):
        return False
    item = mimic_list[item_index]
    if getattr(item, "isCategory", False):
        return False
    if ';' not in item.mimicLineId:
        return False

    target_armature = _resolve_main_armature(context)
    if not target_armature:
        log.warning("No target character armature available for mimic import.")
        return False

    rig_path = _get_mimic_rig_path(target_armature)

    uncook_path = get_uncook_path(context)
    fileName, anim_name = item.mimicLineId.split(';', 1)
    fileName = os.path.join(uncook_path, fileName)
    result = load_bin_anims_single(fileName, anim_name, rigPath=rig_path)
    if not result or not result.animations:
        return False
    animation = result.animations[0]

    if not _ensure_face_morphs_loaded(context, target_armature):
        log.info("Mimic face morphs were not auto-loaded; importing mimic animation anyway.")

    import_anims.import_anim(
        context,
        fileName,
        animation,
        use_NLA=True,
        NLA_track="mimic_import",
        override_select=target_armature,
    )
    return True


class MimicsResourceManager:
    resourceManager = None
    def __init__(self):
        
        RES_DIR = Path(__file__)
        RES_DIR = str(Path(RES_DIR).parents[1])
        filename = os.path.join(RES_DIR, "CR2W", "data", "actor_mimics.csv")
        self.pathashespath = filename

        self.HashdumpDict = {}
        self.MetaByKey = {}
        with open(self.pathashespath, encoding="utf-8", newline="") as csv_file:
            reader = csv.DictReader(csv_file, delimiter=";")
            for row in reader:
                key = row["file"]+";"+row["id"]
                self.HashdumpDict[key] = row["id"]
                try:
                    frames = int((row.get("frames", "") or "0").strip() or 0)
                except Exception:
                    frames = 0
                self.MetaByKey[key] = {
                    "file": row.get("file", "") or "",
                    "cat1": row.get("cat1", "") or "",
                    "cat2": row.get("cat2", "") or "",
                    "cat3": row.get("cat3", "") or "",
                    "caption": row.get("caption", "") or "",
                    "frames": frames,
                }
                #self.HashdumpDict[row["file"]] = row["cat1"]+" "+row["cat2"]+" "+row["cat3"]+": "+row["id"]+" "+row["caption"]+row["frames"]
    @staticmethod
    def Get():
        if (MimicsResourceManager.resourceManager == None):
            MimicsResourceManager.resourceManager = MimicsResourceManager();
        return MimicsResourceManager.resourceManager;




class MyMimicListNode(bpy.types.PropertyGroup):
    name : bpy.props.StringProperty(default="")
    selfIndex : bpy.props.IntProperty(default=-1)
    parentIndex : bpy.props.IntProperty(default=-1)
    childCount : bpy.props.IntProperty(default=0)
    expanded: bpy.props.BoolProperty(default=False)
    isCategory: bpy.props.BoolProperty(default=False)
    categoryKey: bpy.props.StringProperty(default="")
    mimicLineId: bpy.props.StringProperty(default="0000000000")
    filePath: bpy.props.StringProperty(default="")
    cat1: bpy.props.StringProperty(default="")
    cat2: bpy.props.StringProperty(default="")
    cat3: bpy.props.StringProperty(default="")
    caption: bpy.props.StringProperty(default="")
    frames: bpy.props.IntProperty(default=0)
    hint: bpy.props.StringProperty(default="")

class MyMimicListItem(bpy.types.PropertyGroup):
    indent: bpy.props.IntProperty(default=0)
    expanded: bpy.props.BoolProperty(default=False)
    nodeIndex : bpy.props.IntProperty(default=-1) #index into the real tree data.
    childCount: bpy.props.IntProperty(default=0) #should equal myNodes[nodeIndex].childCount
    isCategory: bpy.props.BoolProperty(default=False)
    categoryKey: bpy.props.StringProperty(default="")
    mimicLineId: bpy.props.StringProperty(default="0000000000")
    name: bpy.props.StringProperty(default="")
    filePath: bpy.props.StringProperty(default="")
    cat1: bpy.props.StringProperty(default="")
    cat2: bpy.props.StringProperty(default="")
    cat3: bpy.props.StringProperty(default="")
    caption: bpy.props.StringProperty(default="")
    frames: bpy.props.IntProperty(default=0)
    hint: bpy.props.StringProperty(default="")

def SetupNodeData(context=None):
    scene = _resolve_scene(context)
    if not _scene_has_mimic_props(scene):
        return False
    myNodes = getattr(scene, MIMIC_NODES_PROP)

    expanded_by_category = {}
    for old_node in myNodes:
        if getattr(old_node, "isCategory", False):
            expanded_by_category[str(getattr(old_node, "categoryKey", "") or "")] = bool(getattr(old_node, "expanded", False))

    search_text = str(getattr(scene, MIMIC_SEARCH_PROP, "") or "").strip()
    auto_expand_for_search = bool(search_text)
    node_data = _build_filtered_mimic_nodes(search_text)

    myNodes.clear()
    for item_data in node_data:
        node = myNodes.add()
        node.name = item_data.get("name", "")
        node.selfIndex = len(myNodes) - 1
        node.parentIndex = int(item_data.get("parentIndex", -1))
        node.childCount = int(item_data.get("childCount", 0))
        node.isCategory = bool(item_data.get("isCategory", False))
        node.categoryKey = item_data.get("categoryKey", "")
        node.mimicLineId = item_data.get("mimicLineId", "")
        node.filePath = item_data.get("filePath", "")
        node.cat1 = item_data.get("cat1", "")
        node.cat2 = item_data.get("cat2", "")
        node.cat3 = item_data.get("cat3", "")
        node.caption = item_data.get("caption", "")
        node.frames = int(item_data.get("frames", 0) or 0)
        node.hint = item_data.get("hint", "")
        if node.isCategory:
            node.expanded = auto_expand_for_search or expanded_by_category.get(node.categoryKey, False)
        else:
            node.expanded = False

    log.debug("++++ SetupNodeData ++++")
    log.debug("Node count: %d", len(myNodes))
    return True
        

def NewListItem( mimicList, node):
    item = mimicList.add()
    item.name = node.name
    item.nodeIndex = node.selfIndex
    item.childCount = node.childCount
    item.isCategory = bool(getattr(node, "isCategory", False))
    item.categoryKey = getattr(node, "categoryKey", "")
    item.mimicLineId = node.mimicLineId
    item.filePath = getattr(node, "filePath", "")
    item.cat1 = getattr(node, "cat1", "")
    item.cat2 = getattr(node, "cat2", "")
    item.cat3 = getattr(node, "cat3", "")
    item.caption = getattr(node, "caption", "")
    item.frames = int(getattr(node, "frames", 0) or 0)
    item.hint = getattr(node, "hint", "")
    item.expanded = bool(getattr(node, "expanded", False))
    return item


def SetupListFromNodeData(context=None, preferred_category_key="", preferred_mimic_id=""):
    global _POPULATING_MIMIC_LIST
    scene = _resolve_scene(context)
    if not _scene_has_mimic_props(scene):
        return False
    mimicList = getattr(scene, MIMIC_LIST_PROP)
    old_index = getattr(scene, MIMIC_LIST_INDEX_PROP)
    selected_id = None
    selected_category = ""
    if preferred_mimic_id:
        selected_id = str(preferred_mimic_id)
    elif preferred_category_key:
        selected_category = str(preferred_category_key)
    elif len(mimicList) and 0 <= old_index < len(mimicList):
        selected_item = mimicList[old_index]
        if getattr(selected_item, "isCategory", False):
            selected_category = str(getattr(selected_item, "categoryKey", "") or "")
        else:
            selected_id = selected_item.mimicLineId

    myNodes = getattr(scene, MIMIC_NODES_PROP)
    children_by_parent = {}
    for node in myNodes:
        children_by_parent.setdefault(int(node.parentIndex), []).append(int(node.selfIndex))

    force_expand = bool(str(getattr(scene, MIMIC_SEARCH_PROP, "") or "").strip())

    def _append_node(node_index, indent, new_index_ref):
        node = myNodes[node_index]
        item = NewListItem(mimicList, node)
        item.indent = indent
        item.expanded = bool(getattr(node, "expanded", False) or (force_expand and item.isCategory))
        if selected_id and not item.isCategory and item.mimicLineId == selected_id:
            new_index_ref[0] = len(mimicList) - 1
        elif selected_category and item.isCategory and item.categoryKey == selected_category:
            new_index_ref[0] = len(mimicList) - 1
        if item.childCount > 0 and item.expanded:
            for child_index in children_by_parent.get(node_index, []):
                _append_node(child_index, indent + 1, new_index_ref)

    _POPULATING_MIMIC_LIST = True
    try:
        mimicList.clear()
        new_index_ref = [-1]
        for root_index in children_by_parent.get(-1, []):
            _append_node(root_index, 0, new_index_ref)
        if len(mimicList) == 0:
            setattr(scene, MIMIC_LIST_INDEX_PROP, 0)
        elif new_index_ref[0] >= 0:
            setattr(scene, MIMIC_LIST_INDEX_PROP, new_index_ref[0])
        else:
            setattr(scene, MIMIC_LIST_INDEX_PROP, max(0, min(old_index, len(mimicList) - 1)))
    finally:
        _POPULATING_MIMIC_LIST = False
    return True


def RefreshMimicList(context=None):
    try:
        if not SetupNodeData(context=context):
            return False
        return SetupListFromNodeData(context=context)
    except Exception as exc:
        if _is_id_write_context_error(exc):
            _schedule_deferred_mimic_refresh()
            log.debug("Deferred quick mimic refresh due to restricted ID write context.")
            return False
        raise

def _collapse_descendant_categories(nodes, parent_index):
    for node in nodes:
        if int(getattr(node, "parentIndex", -1)) == parent_index:
            if getattr(node, "isCategory", False):
                node.expanded = False
            _collapse_descendant_categories(nodes, int(getattr(node, "selfIndex", -1)))


def _collapse_category_siblings(nodes, parent_index, selected_index):
    for node in nodes:
        if not getattr(node, "isCategory", False):
            continue
        if int(getattr(node, "parentIndex", -1)) != parent_index:
            continue
        node_index = int(getattr(node, "selfIndex", -1))
        if node_index == selected_index:
            continue
        node.expanded = False
        _collapse_descendant_categories(nodes, node_index)

#
#   Operation to Expand a list item.
#
class MyMimicListItem_Expand(bpy.types.Operator):
    bl_idname = "witcher.quick_mimic_expand" #NOT SURE WHAT TO PUT HERE.
    bl_label = "Tool Name"
    
    button_id: IntProperty(default=0)

    def execute(self, context):
        item_index = self.button_id
        item_list = getattr(context.scene, MIMIC_LIST_PROP)
        if item_index < 0 or item_index >= len(item_list):
            return {'CANCELLED'}
        item = item_list[item_index]
        if not getattr(item, "isCategory", False):
            return {'CANCELLED'}
        selected_category_key = str(getattr(item, "categoryKey", "") or "")

        node_index = int(getattr(item, "nodeIndex", -1))
        myNodes = getattr(context.scene, MIMIC_NODES_PROP)
        if node_index < 0 or node_index >= len(myNodes):
            return {'CANCELLED'}

        node = myNodes[node_index]
        expanding = not bool(getattr(node, "expanded", False))
        if expanding and bool(getattr(context.scene, MIMIC_AUTO_COLLAPSE_PROP, True)):
            _collapse_category_siblings(myNodes, int(getattr(node, "parentIndex", -1)), node_index)
        node.expanded = expanding
        if not expanding:
            _collapse_descendant_categories(myNodes, node_index)
        SetupListFromNodeData(context=context, preferred_category_key=selected_category_key)
        return {'FINISHED'}
    


#
#   Several debug operations
#   (bundled into a single operator with an "action" property)
#
class MyMimicListItem_Debug(bpy.types.Operator):
    bl_idname = "witcher.quick_mimic_debug"
    bl_label = "Debug"

    @classmethod
    def description(cls, context, properties):
        if properties.action == "reset3":
            return "Rebuild quick mimic list"
        if properties.action == "load":
            return "Load currently selected mimic animation"
        if properties.action == "clear_search":
            return "Clear quick mimic search"
        if properties.action == "clear":
            return "Clear quick mimic list"
        return ""
    
    action: StringProperty(default="default")
    
    def execute(self, context):
        scene = context.scene
        action = self.action
        if "load" == action:
            try:
                if not _load_selected_mimic(context):
                    self.report({'WARNING'}, "No mimic selected or could not load mimic animation.")
                    return {'CANCELLED'}
            except FileNotFoundError as e:
                self.report({'ERROR'}, str(e))
                return {'CANCELLED'}
            except Exception as exc:
                log.error("Failed to load selected quick mimic.", exc_info=True)
                self.report({'ERROR'}, str(exc))
                return {'CANCELLED'}
        elif "reset3" == action:
            log.debug("=== Debug Reset ====")
            if hasattr(scene, MIMIC_SEARCH_PROP):
                setattr(scene, MIMIC_SEARCH_PROP, "")
            RefreshMimicList(context=context)
            for node in getattr(scene, MIMIC_NODES_PROP):
                if getattr(node, "isCategory", False):
                    node.expanded = False
            SetupListFromNodeData(context=context)
        elif "clear_search" == action:
            if hasattr(scene, MIMIC_SEARCH_PROP) and getattr(scene, MIMIC_SEARCH_PROP, ""):
                setattr(scene, MIMIC_SEARCH_PROP, "")
            else:
                RefreshMimicList(context=context)
        elif "clear" == action:
            log.debug("=== Debug Clear ====")
            getattr(scene, MIMIC_LIST_PROP).clear()
            getattr(scene, MIMIC_NODES_PROP).clear()
        else:
            log.warning("unknown debug action: %s", action)

        return {'FINISHED'}


class OBJECT_OT_mimic_category_bulk(bpy.types.Operator):
    bl_idname = 'witcher.quick_mimic_category_bulk'
    bl_label = 'Category Bulk'
    bl_description = 'Expand or collapse all quick mimic categories'
    bl_options = {'REGISTER', 'UNDO'}

    action: StringProperty(default="expand_all")

    @classmethod
    def poll(cls, context):
        return bool(getattr(context.scene, MIMIC_NODES_PROP, []))

    def execute(self, context):
        myNodes = getattr(context.scene, MIMIC_NODES_PROP)
        if not myNodes:
            return {'CANCELLED'}
        if self.action == "expand_all":
            for node in myNodes:
                if getattr(node, "isCategory", False):
                    node.expanded = True
        elif self.action == "collapse_all":
            for node in myNodes:
                if getattr(node, "isCategory", False):
                    node.expanded = False
        else:
            return {'CANCELLED'}
        SetupListFromNodeData(context=context)
        return {'FINISHED'}


class MyMimicListItem_Info(bpy.types.Operator):
    bl_idname = "witcher.quick_mimic_info"
    bl_label = "Mimic Details"
    bl_options = {'INTERNAL'}

    mimic_name: StringProperty(default="")
    mimic_id: StringProperty(default="")
    mimic_caption: StringProperty(default="")
    mimic_source: StringProperty(default="")
    mimic_categories: StringProperty(default="")
    mimic_frames: IntProperty(default=0)

    @classmethod
    def description(cls, context, properties):
        parts = []
        if properties.mimic_name:
            parts.append(f"Name: {properties.mimic_name}")
        if properties.mimic_id:
            parts.append(f"Anim ID: {properties.mimic_id}")
        if properties.mimic_caption:
            parts.append(f"Caption: {properties.mimic_caption}")
        if properties.mimic_source:
            parts.append(f"Source: {properties.mimic_source}")
        if properties.mimic_categories:
            parts.append(f"Categories: {properties.mimic_categories}")
        if int(properties.mimic_frames or 0) > 0:
            parts.append(f"Frames: {int(properties.mimic_frames)}")
        return " | ".join(parts) if parts else "Mimic details"

    def execute(self, context):
        return {'FINISHED'}


class MYMIMICLISTITEM_UL_basic(bpy.types.UIList):
    use_filter_show = False
    use_filter_sort_alpha = False

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            frame = layout.row(align=True)
            for _ in range(max(0, int(getattr(item, "indent", 0)))):
                frame.label(text="", icon='BLANK1')

            if getattr(item, "isCategory", False):
                icon_name = 'TRIA_DOWN' if getattr(item, "expanded", False) else 'TRIA_RIGHT'
                op = frame.operator("witcher.quick_mimic_expand", text="", icon=icon_name, emboss=False)
                op.button_id = index
                text_row = frame.row(align=True)
                text_row.alignment = 'LEFT'
                label_op = text_row.operator("witcher.quick_mimic_expand", text=item.name, emboss=False, icon='NONE')
                label_op.button_id = index
            else:
                frame.label(text=item.name)
                hint_text = (getattr(item, "hint", "") or "").strip()
                match_icon = _get_mimic_match_icon(context, item)
                if match_icon:
                    frame.label(text="", icon=match_icon)
                if hint_text:
                    frame.label(text=hint_text)

                anim_id = ""
                if ";" in str(getattr(item, "mimicLineId", "")):
                    _file_name, anim_id = str(item.mimicLineId).split(";", 1)
                categories = " / ".join(
                    [c for c in (str(item.cat1).strip(), str(item.cat2).strip(), str(item.cat3).strip()) if c]
                )
                info_op = frame.operator("witcher.quick_mimic_info", text="", icon='INFO', emboss=False)
                info_op.mimic_name = str(getattr(item, "name", "") or "")
                info_op.mimic_id = str(anim_id or "")
                info_op.mimic_caption = str(getattr(item, "caption", "") or "")
                info_op.mimic_source = str(getattr(item, "filePath", "") or "")
                info_op.mimic_categories = categories
                info_op.mimic_frames = int(getattr(item, "frames", 0) or 0)
        else:
            layout.label(text=item.name)

    def draw_filter(self, context, layout):
        pass

    def filter_items(self, context, data, propname):
        return [], []


def ensure_mimic_list_initialized(context):
    scene = getattr(context, "scene", None)
    if scene is None:
        return
    nodes = getattr(scene, MIMIC_NODES_PROP, None)
    items = getattr(scene, MIMIC_LIST_PROP, None)
    if nodes is None or items is None:
        return
    if len(nodes) == 0 and len(items) == 0:
        _schedule_deferred_mimic_refresh()


from ..ui.ui_utils import WITCH_PT_Base
class SCENE_PT_witcher_mimic_list(WITCH_PT_Base, bpy.types.Panel):
    bl_parent_id = "WITCH_PT_Quick"
    bl_label = "Quick Mimic List"
    bl_idname = "SCENE_PT_witcher_mimic_list"

    def draw(self, context):
        ensure_mimic_list_initialized(context)
        scn = context.scene
        layout = self.layout

        search_row = layout.row(align=True)
        search_row.prop(scn, MIMIC_SEARCH_PROP, text="", icon='VIEWZOOM')
        clear_btn = search_row.row(align=True)
        clear_btn.enabled = bool(getattr(scn, MIMIC_SEARCH_PROP, ""))
        clear_btn.operator("witcher.quick_mimic_debug", text="", icon='X').action = "clear_search"

        control_row = layout.row(align=True)
        control_row.prop(scn, MIMIC_AUTO_LOAD_PROP, text="Load on Select")
        control_row.operator("witcher.quick_mimic_debug", text="Reset", icon='FILE_REFRESH').action = "reset3"
        control_row.operator("witcher.quick_mimic_debug", text="Load", icon='PLAY').action = "load"

        layout.prop(scn, MIMIC_AUTO_COLLAPSE_PROP, text="Auto Collapse Categories")

        bulk_row = layout.row(align=True)
        bulk_row.operator("witcher.quick_mimic_category_bulk", text="Expand All").action = "expand_all"
        bulk_row.operator("witcher.quick_mimic_category_bulk", text="Collapse All").action = "collapse_all"

        list_box = layout.box()
        list_box.template_list(
            "MYMIMICLISTITEM_UL_basic",
            "W3_UI_MIMIC_LIST_PANEL",
            scn,
            MIMIC_LIST_PROP,
            scn,
            MIMIC_LIST_INDEX_PROP,
            sort_lock=True,
            rows=8,
        )
        list_box.label(text=f"{len(getattr(scn, MIMIC_LIST_PROP, []))} visible entries", icon='INFO')
        

classes = (
        MyMimicListNode,
        MyMimicListItem,
        MyMimicListItem_Expand,
        MyMimicListItem_Debug,
        OBJECT_OT_mimic_category_bulk,
        MyMimicListItem_Info,
        MYMIMICLISTITEM_UL_basic,
        SCENE_PT_witcher_mimic_list)


def on_mimic_list_index_changed(self, context):
    global _AUTO_LOADING_MIMIC
    if _AUTO_LOADING_MIMIC or _POPULATING_MIMIC_LIST:
        return
    if context is None or getattr(context, "scene", None) is None:
        return
    if not getattr(context.scene, MIMIC_AUTO_LOAD_PROP, False):
        return
    mimic_list = getattr(context.scene, MIMIC_LIST_PROP, None)
    current_index = int(getattr(context.scene, MIMIC_LIST_INDEX_PROP, -1))
    if current_index < 0 or mimic_list is None or len(mimic_list) == 0 or current_index >= len(mimic_list):
        return
    current_item = mimic_list[current_index]
    if getattr(current_item, "isCategory", False):
        return
    try:
        _AUTO_LOADING_MIMIC = True
        _load_selected_mimic(context)
    except Exception as exc:
        log.error("Quick mimic load-on-select failed: %s", exc)
    finally:
        _AUTO_LOADING_MIMIC = False


def on_mimic_search_changed(self, context):
    if context is None or getattr(context, "scene", None) is None:
        return
    RefreshMimicList(context=context)


def on_mimic_auto_collapse_categories_changed(self, context):
    if context is None or getattr(context, "scene", None) is None:
        return
    if not getattr(context.scene, MIMIC_AUTO_COLLAPSE_PROP, True):
        return
    myNodes = getattr(context.scene, MIMIC_NODES_PROP, None)
    if not myNodes:
        return
    selected_parent = {}
    for node in myNodes:
        if not getattr(node, "isCategory", False):
            continue
        if not getattr(node, "expanded", False):
            continue
        parent_index = int(getattr(node, "parentIndex", -1))
        node_index = int(getattr(node, "selfIndex", -1))
        if parent_index not in selected_parent:
            selected_parent[parent_index] = node_index
            continue
        if selected_parent[parent_index] != node_index:
            node.expanded = False
            _collapse_descendant_categories(myNodes, node_index)
    SetupListFromNodeData(context=context)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    if not hasattr(bpy.types.Scene, MIMIC_NODES_PROP):
        setattr(bpy.types.Scene, MIMIC_NODES_PROP, bpy.props.CollectionProperty(type=MyMimicListNode))
    if not hasattr(bpy.types.Scene, MIMIC_LIST_PROP):
        setattr(bpy.types.Scene, MIMIC_LIST_PROP, bpy.props.CollectionProperty(type=MyMimicListItem))
    if not hasattr(bpy.types.Scene, MIMIC_LIST_INDEX_PROP):
        setattr(bpy.types.Scene, MIMIC_LIST_INDEX_PROP, IntProperty(update=on_mimic_list_index_changed))
    if not hasattr(bpy.types.Scene, MIMIC_AUTO_LOAD_PROP):
        setattr(bpy.types.Scene, MIMIC_AUTO_LOAD_PROP, BoolProperty(
            name="Load on Select",
            description="Automatically load mimic when selecting it in the quick mimic list",
            default=True,
        ))
    if not hasattr(bpy.types.Scene, MIMIC_AUTO_COLLAPSE_PROP):
        setattr(bpy.types.Scene, MIMIC_AUTO_COLLAPSE_PROP, BoolProperty(
            name="Auto Collapse Categories",
            description="When enabled, opening one mimic category collapses its siblings",
            default=True,
            update=on_mimic_auto_collapse_categories_changed,
        ))
    if not hasattr(bpy.types.Scene, MIMIC_SEARCH_PROP):
        setattr(bpy.types.Scene, MIMIC_SEARCH_PROP, StringProperty(
            name="",
            description="Search mimics",
            default="",
            update=on_mimic_search_changed,
        ))


def unregister():
    global _MIMIC_NODE_CACHE, _MIMIC_REFRESH_DEFERRED
    _MIMIC_NODE_CACHE = None
    _MIMIC_REFRESH_DEFERRED = False
    try:
        if bpy.app.timers.is_registered(_deferred_refresh_mimic_list):
            bpy.app.timers.unregister(_deferred_refresh_mimic_list)
    except Exception:
        pass
    if hasattr(bpy.types.Scene, MIMIC_SEARCH_PROP):
        delattr(bpy.types.Scene, MIMIC_SEARCH_PROP)
    if hasattr(bpy.types.Scene, MIMIC_AUTO_COLLAPSE_PROP):
        delattr(bpy.types.Scene, MIMIC_AUTO_COLLAPSE_PROP)
    if hasattr(bpy.types.Scene, MIMIC_AUTO_LOAD_PROP):
        delattr(bpy.types.Scene, MIMIC_AUTO_LOAD_PROP)
    if hasattr(bpy.types.Scene, MIMIC_LIST_INDEX_PROP):
        delattr(bpy.types.Scene, MIMIC_LIST_INDEX_PROP)
    if hasattr(bpy.types.Scene, MIMIC_LIST_PROP):
        delattr(bpy.types.Scene, MIMIC_LIST_PROP)
    if hasattr(bpy.types.Scene, MIMIC_NODES_PROP):
        delattr(bpy.types.Scene, MIMIC_NODES_PROP)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
