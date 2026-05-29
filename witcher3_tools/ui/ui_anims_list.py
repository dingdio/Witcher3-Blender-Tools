import logging
from pathlib import Path
from .. import import_anims
#from io_import_w2l.filter_list import memory
log = logging.getLogger(__name__)
from ..CR2W.dc_anims import load_bin_anims_single
from ..source_game_paths import (
    normalize_source_game,
    repo_file_for_source,
    resolve_w2_repo_file_from_source,
    source_game_for_rig_settings,
    source_roots,
)

import csv
import os
import re
import bpy
from bpy.types import PropertyGroup
from bpy.props import (
    CollectionProperty,
    IntProperty,
    BoolProperty,
    EnumProperty,
    StringProperty,
    PointerProperty,
)
from ..ui.armature_context import get_main_armature, set_main_armature
from ..CR2W.scene_csv_utils import (
    _lookup_dialogset_body_anim,
    _parse_body_anim_csv,
)


DIALOGSET_BODY_STATUS_PROP = "witcher_quick_dialogset_body_status"
DIALOGSET_BODY_EMOTIONAL_PROP = "witcher_quick_dialogset_body_emotional_state"
DIALOGSET_BODY_POSE_PROP = "witcher_quick_dialogset_body_pose_name"
DIALOGSET_BODY_RESOLVED_STATUS_PROP = "witcher_quick_dialogset_body_resolved_status"
DIALOGSET_BODY_RESOLVED_ANIM_PROP = "witcher_quick_dialogset_body_resolved_animation"
DIALOGSET_BODY_TRACK = "SceneDialogsetIdle"
_DIALOGSET_BODY_ITEMS_CACHE = None
_DIALOGSET_BODY_RESOLVE_CACHE = {}
_UPDATING_DIALOGSET_BODY = False

W2_PLAYER_ENTITY_PATH = r"characters\templates\witcher\player.w2ent"
W2_PLAYER_FALLBACK_ANIMSETS = (
    r"characters\templates\interaction\carry\carry_arian__witcher.w2anims",
    r"characters\templates\interaction\dialog\d_sit.w2anims",
    r"characters\templates\interaction\dialog\d_stand.w2anims",
    r"characters\templates\interaction\dialog\d_stand_agressive.w2anims",
    r"characters\templates\interaction\dialog\d_stand_assured.w2anims",
    r"characters\templates\interaction\dialog\d_stand_expressive.w2anims",
    r"characters\templates\interaction\dialog\d_stand_insincere.w2anims",
    r"characters\templates\interaction\dialog\d_stand_intimidiate.w2anims",
    r"characters\templates\interaction\dialog\d_stand_submissive.w2anims",
    r"characters\templates\interaction\dialog\d_stand_unwilling.w2anims",
    r"characters\templates\interaction\fistfight\fistfight_witcher.w2anims",
    r"characters\templates\interaction\gameplay\gameplay_man.w2anims",
    r"characters\templates\interaction\takedowns\fin_1man\witcher.w2anims",
    r"characters\templates\interaction\takedowns\fin_2man\witcher.w2anims",
    r"characters\templates\interaction\takedowns\takedowns_man\takedowns_witcher.w2anims",
    r"characters\templates\interaction\wrist_wrestling\wrist_wrestling_witcher.w2anims",
    r"characters\templates\mimics\base_mimics.w2anims",
    r"characters\templates\mimics\custom_gest.w2anims",
    r"characters\templates\mimics\gest.w2anims",
    r"characters\templates\witcher\animation\c_combo.w2anims",
    r"characters\templates\witcher\animation\c_fsv.w2anims",
    r"characters\templates\witcher\animation\c_guards.w2anims",
    r"characters\templates\witcher\animation\c_st.w2anims",
    r"characters\templates\witcher\animation\c_sword.w2anims",
    r"characters\templates\witcher\animation\combat_signs.w2anims",
    r"characters\templates\witcher\animation\combat_zagnica.w2anims",
    r"characters\templates\witcher\animation\containers.w2anims",
    r"characters\templates\witcher\animation\dragon_a3_combat.w2anims",
    r"characters\templates\witcher\animation\exploration_animset.w2anims",
    r"characters\templates\witcher\animation\meditation.w2anims",
    r"characters\templates\witcher\animation\obstacle.w2anims",
    r"characters\templates\witcher\animation\prisoner.w2anims",
    r"characters\templates\witcher\animation\prototype.w2anims",
    r"characters\templates\witcher\animation\quest.w2anims",
    r"characters\templates\witcher\animation\weapon_draw.w2anims",
    r"characters\templates\witcher\animation\witcher_fistfight.w2anims",
    r"characters\templates\witcher\animation\witcher_stealth.w2anims",
    r"characters\templates\witcher\animation\witcher_steel.w2anims",
)
_W2_PLAYER_ANIMSETS_CACHE = {}
_W2_ANIMSET_PATH_BYTES_RE = re.compile(rb"[A-Za-z0-9_./\\-]+\.w2anims", re.IGNORECASE)
_W2_ANIMSET_PATH_TEXT_RE = re.compile(r"[A-Za-z0-9_./\\-]+\.w2anims", re.IGNORECASE)


def _source_game_for_armature_obj(armature_obj, fallback="w3"):
    rig_settings = getattr(getattr(armature_obj, "data", None), "witcherui_RigSettings", None)
    rig_value = str(getattr(rig_settings, "source_game", "") or "").strip() if rig_settings else ""
    if rig_value:
        return source_game_for_rig_settings(rig_settings, fallback=fallback)
    try:
        obj_value = str(armature_obj.get("witcher_source_game", "") or "").strip()
    except Exception:
        obj_value = ""
    return normalize_source_game(obj_value or fallback)


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


def _armature_identity_text(armature_obj) -> str:
    if armature_obj is None:
        return ""
    parts = [str(getattr(armature_obj, "name", "") or "")]
    for prop_name in ("witcher_path", "witcher_source_game"):
        try:
            value = str(armature_obj.get(prop_name, "") or "").strip()
        except Exception:
            value = ""
        if value:
            parts.append(value)
    rig_settings = getattr(getattr(armature_obj, "data", None), "witcherui_RigSettings", None)
    if rig_settings:
        for attr_name in ("entity_name", "repo_path", "main_entity_skeleton", "main_face_skeleton"):
            value = str(getattr(rig_settings, attr_name, "") or "").strip()
            if value:
                parts.append(value)
    return " ".join(parts).lower().replace("/", "\\")


def _target_looks_like_w3_player(armature_obj) -> bool:
    if armature_obj is None or getattr(armature_obj, "type", None) != "ARMATURE":
        return False
    if _source_game_for_armature_obj(armature_obj) == "w2":
        return False
    text = _armature_identity_text(armature_obj)
    if "gameplay\\templates\\characters\\player\\player.w2ent" in text:
        return True
    if "\\player\\player.w2ent" in text and "ciri" not in text:
        return True
    is_man_base = "characters\\base_entities\\man_base\\man_base.w2rig" in text
    return is_man_base and any(term in text for term in ("geralt", "player", "witcher"))


def _uses_w2_player_animsets(main_arm_obj, source_game="") -> bool:
    return normalize_source_game(source_game) == "w2" and _target_looks_like_w3_player(main_arm_obj)


def _clean_w2_animset_path(path_value) -> str:
    text = str(path_value or "").replace("/", "\\").strip().strip("\x00").lstrip("\\")
    lower_text = text.lower()
    marker = "characters\\"
    marker_index = lower_text.find(marker)
    if marker_index >= 0:
        text = text[marker_index:]
        lower_text = text.lower()
    if not lower_text.endswith(".w2anims"):
        return ""
    return text


def _iter_w2_player_entity_candidates(context):
    seen = set()

    def emit(path_value):
        path_value = str(path_value or "").strip()
        if not path_value:
            return
        key = os.path.normcase(os.path.normpath(path_value))
        if key in seen or not os.path.exists(path_value):
            return
        seen.add(key)
        yield path_value

    try:
        for root in source_roots(context, "w2", existing_only=True):
            yield from emit(os.path.join(root, W2_PLAYER_ENTITY_PATH))
    except Exception:
        pass
    try:
        yield from emit(repo_file_for_source(W2_PLAYER_ENTITY_PATH, "w2"))
    except Exception:
        pass


def _scan_w2_animsets_from_entity(path_value):
    try:
        with open(path_value, "rb") as handle:
            data = handle.read()
    except Exception:
        return ()

    hits = set()
    for raw_path in _W2_ANIMSET_PATH_BYTES_RE.findall(data):
        try:
            clean = _clean_w2_animset_path(raw_path.decode("ascii", "ignore"))
        except Exception:
            clean = ""
        if clean:
            hits.add(clean)

    try:
        text = data.decode("utf-16le", "ignore")
    except Exception:
        text = ""
    for raw_path in _W2_ANIMSET_PATH_TEXT_RE.findall(text):
        clean = _clean_w2_animset_path(raw_path)
        if clean:
            hits.add(clean)

    return tuple(sorted(hits, key=str.lower))


def _w2_player_animsets(context=None):
    candidates = tuple(_iter_w2_player_entity_candidates(context))
    cache_key = []
    for path_value in candidates:
        try:
            stat = os.stat(path_value)
            cache_key.append((os.path.normcase(os.path.normpath(path_value)), int(stat.st_mtime), int(stat.st_size)))
        except Exception:
            cache_key.append((os.path.normcase(os.path.normpath(path_value)), 0, 0))
    cache_key = tuple(cache_key)
    cached = _W2_PLAYER_ANIMSETS_CACHE.get(cache_key)
    if cached is not None:
        return cached

    paths = set()
    for path_value in candidates:
        paths.update(_scan_w2_animsets_from_entity(path_value))
    if not paths:
        paths.update(W2_PLAYER_FALLBACK_ANIMSETS)

    result = tuple(sorted(paths, key=str.lower))
    _W2_PLAYER_ANIMSETS_CACHE[cache_key] = result
    return result


def _dialogset_body_item_tuple(values, fallback):
    clean_values = sorted({str(value or "").strip() for value in values or [] if str(value or "").strip()})
    return tuple((value, value, "") for value in clean_values) or ((fallback, fallback, ""),)


def _dialogset_body_items_cache():
    global _DIALOGSET_BODY_ITEMS_CACHE
    if _DIALOGSET_BODY_ITEMS_CACHE is not None:
        return _DIALOGSET_BODY_ITEMS_CACHE

    data = _parse_body_anim_csv()
    statuses = set()
    emotional_by_status = {}
    poses_by_status_emotional = {}
    all_emotional = set()
    all_poses = set()

    for (status_key, emotional_key, pose_key), entry in data.items():
        if not (entry.get("idles") or []):
            continue
        status = str(entry.get("status_display", status_key) or status_key).strip()
        emotional = str(entry.get("emotional_display", emotional_key) or emotional_key).strip()
        pose = str(entry.get("pose_display", pose_key) or pose_key).strip()
        if not status or not emotional or not pose:
            continue
        status_l = status.lower()
        emotional_l = emotional.lower()
        statuses.add(status)
        all_emotional.add(emotional)
        all_poses.add(pose)
        emotional_by_status.setdefault(status_l, set()).add(emotional)
        poses_by_status_emotional.setdefault((status_l, emotional_l), set()).add(pose)

    _DIALOGSET_BODY_ITEMS_CACHE = {
        "statuses": _dialogset_body_item_tuple(statuses, "High"),
        "all_emotional": _dialogset_body_item_tuple(all_emotional, "Determined"),
        "all_poses": _dialogset_body_item_tuple(all_poses, "Standing"),
        "emotional_by_status": {
            status: _dialogset_body_item_tuple(values, "Determined")
            for status, values in emotional_by_status.items()
        },
        "poses_by_status_emotional": {
            key: _dialogset_body_item_tuple(values, "Standing")
            for key, values in poses_by_status_emotional.items()
        },
    }
    return _DIALOGSET_BODY_ITEMS_CACHE


def _enum_item_ids(items):
    return {str(item[0]) for item in items or []}


def _first_enum_item_id(items):
    for item in items or []:
        return str(item[0])
    return ""


def _dialogset_body_status_items(self, context):
    return _dialogset_body_items_cache()["statuses"]


def _dialogset_body_emotional_items(self, context):
    scene = getattr(context, "scene", None) if context else None
    status = str(getattr(scene, DIALOGSET_BODY_STATUS_PROP, "") or "").strip().lower()
    cache = _dialogset_body_items_cache()
    return cache["emotional_by_status"].get(status) or cache["all_emotional"]


def _dialogset_body_pose_items(self, context):
    scene = getattr(context, "scene", None) if context else None
    status = str(getattr(scene, DIALOGSET_BODY_STATUS_PROP, "") or "").strip().lower()
    emotional = str(getattr(scene, DIALOGSET_BODY_EMOTIONAL_PROP, "") or "").strip().lower()
    cache = _dialogset_body_items_cache()
    return cache["poses_by_status_emotional"].get((status, emotional)) or cache["all_poses"]


def _set_scene_prop_if_changed(scene, prop_name, value):
    if scene is None or not hasattr(scene, prop_name):
        return
    value = str(value or "")
    if str(getattr(scene, prop_name, "") or "") != value:
        setattr(scene, prop_name, value)


def _clear_dialogset_body_preview(scene):
    if scene is None:
        return
    if hasattr(scene, DIALOGSET_BODY_RESOLVED_STATUS_PROP):
        setattr(scene, DIALOGSET_BODY_RESOLVED_STATUS_PROP, "Selection changed; resolve before loading.")
    if hasattr(scene, DIALOGSET_BODY_RESOLVED_ANIM_PROP):
        setattr(scene, DIALOGSET_BODY_RESOLVED_ANIM_PROP, "")


def _sync_dialogset_body_selection(scene, context, sync_emotional=True):
    global _UPDATING_DIALOGSET_BODY
    if scene is None:
        return
    try:
        _UPDATING_DIALOGSET_BODY = True
        if sync_emotional:
            emotional_items = _dialogset_body_emotional_items(None, context)
            current_emotional = str(getattr(scene, DIALOGSET_BODY_EMOTIONAL_PROP, "") or "")
            if current_emotional not in _enum_item_ids(emotional_items):
                _set_scene_prop_if_changed(scene, DIALOGSET_BODY_EMOTIONAL_PROP, _first_enum_item_id(emotional_items))

        pose_items = _dialogset_body_pose_items(None, context)
        current_pose = str(getattr(scene, DIALOGSET_BODY_POSE_PROP, "") or "")
        if current_pose not in _enum_item_ids(pose_items):
            _set_scene_prop_if_changed(scene, DIALOGSET_BODY_POSE_PROP, _first_enum_item_id(pose_items))
    finally:
        _UPDATING_DIALOGSET_BODY = False


def on_dialogset_body_status_changed(self, context):
    if _UPDATING_DIALOGSET_BODY or context is None:
        return
    scene = getattr(context, "scene", None)
    _sync_dialogset_body_selection(scene, context, sync_emotional=True)
    _clear_dialogset_body_preview(scene)


def on_dialogset_body_emotional_changed(self, context):
    if _UPDATING_DIALOGSET_BODY or context is None:
        return
    scene = getattr(context, "scene", None)
    _sync_dialogset_body_selection(scene, context, sync_emotional=False)
    _clear_dialogset_body_preview(scene)


def on_dialogset_body_pose_changed(self, context):
    if _UPDATING_DIALOGSET_BODY or context is None:
        return
    _clear_dialogset_body_preview(getattr(context, "scene", None))


def _dialogset_body_selection(scene):
    return (
        str(getattr(scene, DIALOGSET_BODY_STATUS_PROP, "") or "").strip(),
        str(getattr(scene, DIALOGSET_BODY_EMOTIONAL_PROP, "") or "").strip(),
        str(getattr(scene, DIALOGSET_BODY_POSE_PROP, "") or "").strip(),
    )


def _format_dialogset_body_resolved(info):
    if not info:
        return ""
    anim_id = str(info.get("anim_id", "") or "").strip()
    resolved_name = str(info.get("resolved_anim_name", "") or "").strip()
    resolved_path = str(info.get("resolved_path", "") or "").strip()
    if not anim_id:
        return ""
    if not resolved_name:
        return f"{anim_id}: <not resolved>"
    return f"{resolved_name} [{os.path.basename(resolved_path)}]"


def _set_dialogset_body_preview(scene, info=None, status=""):
    if scene is None:
        return
    if hasattr(scene, DIALOGSET_BODY_RESOLVED_STATUS_PROP):
        setattr(scene, DIALOGSET_BODY_RESOLVED_STATUS_PROP, str(status or ""))
    if hasattr(scene, DIALOGSET_BODY_RESOLVED_ANIM_PROP):
        setattr(scene, DIALOGSET_BODY_RESOLVED_ANIM_PROP, _format_dialogset_body_resolved(info or {}))


def resolve_quick_dialogset_body(context, force=False):
    scene = getattr(context, "scene", None)
    actor_obj = _resolve_main_armature(context)
    if scene is None:
        return None, {}

    status, emotional, pose = _dialogset_body_selection(scene)
    anim_id = _lookup_dialogset_body_anim(status, emotional, pose) or ""
    info = {
        "actor_status": status,
        "actor_emotional_state": emotional,
        "actor_pose_name": pose,
        "anim_id": anim_id,
        "resolved_anim_name": "",
        "resolved_path": "",
    }
    if actor_obj is None or not anim_id:
        return actor_obj, info

    show_all = False
    source_override = _resolve_quick_anim_source(scene, actor_obj)
    source_key = _get_quick_anim_source_key(actor_obj, show_all, source_override=source_override)
    cache_key = (source_key, status.lower(), emotional.lower(), pose.lower(), anim_id)
    cached = _DIALOGSET_BODY_RESOLVE_CACHE.get(cache_key)
    if cached is not None and not force:
        return actor_obj, dict(cached)

    SetupActor(actor_obj, context=context, show_all=show_all)
    resolved_anim_name, fdir = GetAnimationInfoByName(
        anim_id,
        actor_obj,
        show_all=show_all,
        quiet=True,
        compatible_only=True,
        source_game=source_override,
    )
    if resolved_anim_name and fdir:
        info["resolved_anim_name"] = str(resolved_anim_name)
        info["resolved_path"] = str(fdir)

    _DIALOGSET_BODY_RESOLVE_CACHE[cache_key] = dict(info)
    if len(_DIALOGSET_BODY_RESOLVE_CACHE) > 256:
        _DIALOGSET_BODY_RESOLVE_CACHE.clear()
    return actor_obj, info


def resolve_and_store_quick_dialogset_body(context):
    scene = getattr(context, "scene", None)
    actor_obj, info = resolve_quick_dialogset_body(context, force=True)
    if scene is None:
        return None, {}
    if actor_obj is None:
        _set_dialogset_body_preview(scene, info, status="No target character selected.")
        return actor_obj, info
    if not info.get("anim_id"):
        _set_dialogset_body_preview(scene, info, status="No body animation found for this combination.")
        return actor_obj, info
    if not info.get("resolved_anim_name"):
        _set_dialogset_body_preview(scene, info, status="Body animation is not compatible with the current character's animation sets.")
        return actor_obj, info
    _set_dialogset_body_preview(scene, info, status="Resolved.")
    return actor_obj, info


def load_quick_dialogset_body(context):
    scene = getattr(context, "scene", None)
    if scene is None:
        return False
    actor_obj, info = resolve_quick_dialogset_body(context, force=True)
    if actor_obj is None:
        _set_dialogset_body_preview(scene, info, status="No target character selected.")
        return False
    if not info.get("anim_id"):
        _set_dialogset_body_preview(scene, info, status="No body animation found for this combination.")
        return False
    if not info.get("resolved_anim_name") or not info.get("resolved_path"):
        _set_dialogset_body_preview(scene, info, status="Body animation is not compatible with the current character's animation sets.")
        return False
    _set_dialogset_body_preview(scene, info, status="Resolved.")

    mode_map = {'REPLACE': 'replace', 'APPEND': 'append', 'APPEND_AT_CURSOR': 'append_at_cursor'}
    nla_mode = mode_map.get(getattr(scene, 'witcher_anim_nla_mode', 'REPLACE'), 'replace')
    load_anim_into_scene(
        context,
        info["resolved_anim_name"],
        info["resolved_path"],
        actor_obj,
        NLA_track=DIALOGSET_BODY_TRACK,
        at_frame=0,
        nla_mode=nla_mode,
    )
    if getattr(scene, "witcher_auto_orient_root", True):
        try:
            from ..ui.ui_anims import apply_root_orientation
            apply_root_orientation(actor_obj)
        except Exception as exc:
            log.warning("Dialogset body auto orient failed: %s", exc)
    return True


def _draw_resolved_dialogset_body(layout, context):
    scene = getattr(context, "scene", None)
    preview = layout.column(align=True)
    preview.label(text="Resolved Animation", icon='PREVIEW_RANGE')
    if scene is None:
        return
    status = str(getattr(scene, DIALOGSET_BODY_RESOLVED_STATUS_PROP, "") or "").strip()
    resolved = str(getattr(scene, DIALOGSET_BODY_RESOLVED_ANIM_PROP, "") or "").strip()
    if status:
        preview.label(text=status, icon='INFO')
    if resolved:
        preview.label(text=f"{DIALOGSET_BODY_TRACK}: {resolved}", icon='ANIM_DATA')
    if not status and not resolved:
        preview.label(text="Use Resolve Body to preview before loading.", icon='INFO')


def draw_quick_dialogset_body_controls(layout, context):
    scene = getattr(context, "scene", None)
    if scene is None:
        return
    required = (
        DIALOGSET_BODY_STATUS_PROP,
        DIALOGSET_BODY_EMOTIONAL_PROP,
        DIALOGSET_BODY_POSE_PROP,
    )
    if not all(hasattr(scene, prop) for prop in required):
        layout.label(text="Dialogset body properties not registered.", icon='INFO')
        return

    col = layout.column(align=True)
    col.prop(scene, DIALOGSET_BODY_STATUS_PROP, text="actorStatus")
    col.prop(scene, DIALOGSET_BODY_EMOTIONAL_PROP, text="actorEmotionalState")
    col.prop(scene, DIALOGSET_BODY_POSE_PROP, text="actorPoseName")
    _draw_resolved_dialogset_body(col, context)
    actions = col.row(align=True)
    actions.operator("witcher.quick_dialogset_body_resolve", text="Resolve Body", icon='PREVIEW_RANGE')
    actions.operator("witcher.quick_dialogset_body_load", text="Load Resolved", icon='PLAY')


def is_face_animation(anim_name, fdir=""):
    anim_text = str(anim_name or "").strip().lower()
    if ":face" in anim_text:
        return True
    if anim_text.endswith("_face") or "_face:" in anim_text:
        return True
    path_text = str(fdir or "").strip().replace("/", "\\").lower()
    if "_mimic_" in path_text or "\\mimics\\" in path_text:
        return True
    if "lipsync" in anim_text or "lipsync" in path_text:
        return True
    return False


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
    # Witcher 2 support: imported CW2MimicHeadComponent armatures carry W2
    # mimic metadata instead of W3 mimicFace/mimicFaceFile fields.
    if bool(obj.get("witcher_w2_mimic_support", False)):
        w2_mimic_armature = str(obj.get("witcher_w2_mimic_armature", "") or "").strip()
        if w2_mimic_armature and w2_mimic_armature == object_name:
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
        mimic_name = str(root_obj.get("witcher_w2_mimic_armature", "") or "").strip()
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
    if not mimic_targets and bool(main_arm_obj.get("witcher_w2_mimic_support", False)):
        mimic_targets.append(main_arm_obj)
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


def ensure_face_animation_setup(context, main_arm_obj, target_armatures=None, force=False):
    track_target_armature = _resolve_face_track_target_armature(main_arm_obj, target_armatures or [])
    if track_target_armature is None:
        return False, None
    if _is_mimic_component_armature(track_target_armature):
        return False, track_target_armature

    try:
        from .ui_mimics import _ensure_face_morphs_loaded

        return bool(_ensure_face_morphs_loaded(context, track_target_armature, force=force)), track_target_armature
    except Exception:
        log.warning(
            "Failed to ensure face morph setup on '%s'.",
            getattr(track_target_armature, "name", "<unknown>"),
            exc_info=True,
        )
        return False, track_target_armature


def ensure_owner_face_animation_setup(context, main_arm_obj=None, force=False):
    _resolved_main_arm_obj, owner_armature, _rig_path = resolve_owner_face_animation_context(context, main_arm_obj)
    try:
        from .ui_mimics import _ensure_face_morphs_loaded

        return bool(_ensure_face_morphs_loaded(context, owner_armature, force=force)), owner_armature
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
        try:
            candidate = str(main_arm_obj.get("witcher_path", "") or "").strip()
        except Exception:
            candidate = ""
        if candidate.lower().endswith((".w2rig", ".w3dyng", ".dyng")):
            skeleton_path = candidate
    if not skeleton_path:
        return None
    try:
        return repo_file_for_source(skeleton_path, _source_game_for_armature_obj(main_arm_obj))
    except Exception:
        return None


def _iter_object_descendants(root_obj):
    stack = list(getattr(root_obj, "children", []) or [])
    while stack:
        obj = stack.pop(0)
        yield obj
        stack.extend(list(getattr(obj, "children", []) or []))


def _resolve_component_armature(main_arm_obj, component_name):
    component_key = str(component_name or "").strip().lower()
    if not component_key or component_key in {"root", "body"}:
        return main_arm_obj
    if component_key in {"face", "mimic"}:
        mimic_name = str(main_arm_obj.get("mimicFace", "") or "").strip() if main_arm_obj else ""
        mimic_obj = bpy.data.objects.get(mimic_name) if mimic_name else None
        if getattr(mimic_obj, "type", None) == "ARMATURE":
            return mimic_obj

    candidates = [main_arm_obj] + list(_iter_object_descendants(main_arm_obj))
    for obj in candidates:
        if getattr(obj, "type", None) != "ARMATURE":
            continue
        try:
            obj_component = str(obj.get("witcher_name", "") or "").strip().lower()
        except Exception:
            obj_component = ""
        if obj_component and obj_component == component_key:
            return obj
    return main_arm_obj


def _resolve_face_rig_path(main_arm_obj, target_armatures):
    candidates = []
    if main_arm_obj:
        candidates.append(main_arm_obj)
    candidates.extend(target_armatures or [])
    main_source_game = _source_game_for_armature_obj(main_arm_obj)
    for armature_obj in _unique_armatures(candidates):
        w2_track_skeleton = str(armature_obj.get("witcher_w2_mimic_float_track_skeleton", "") or "").strip()
        mimic_face_file = str(armature_obj.get("mimicFaceFile", "") or "").strip()
        source_game = _source_game_for_armature_obj(armature_obj, fallback=main_source_game)
        if w2_track_skeleton:
            try:
                return repo_file_for_source(w2_track_skeleton, source_game)
            except Exception:
                pass
        if mimic_face_file:
            try:
                return repo_file_for_source(mimic_face_file, source_game)
            except Exception:
                pass
        rig_settings = getattr(armature_obj.data, "witcherui_RigSettings", None)
        skeleton_path = str(getattr(rig_settings, "main_face_skeleton", "") or "").strip() if rig_settings else ""
        if skeleton_path:
            try:
                return repo_file_for_source(skeleton_path, source_game)
            except Exception:
                pass
    return None


def resolve_animation_load_context(context, anim_name, fdir="", main_arm_obj=None, target_component=""):
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
        target_armature = _resolve_component_armature(main_arm_obj, target_component)
        target_armatures = [target_armature]
        rig_path = _resolve_entity_rig_path(target_armature) or _resolve_entity_rig_path(main_arm_obj)
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
    source_override = _resolve_quick_anim_source(scene, main_arm_obj)
    current_key = _get_quick_anim_source_key(main_arm_obj, show_all, source_override=source_override)
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


def _quick_anim_source_pref(scene) -> str:
    """Return the user's source-game override: AUTO / W3 / W2."""
    return str(getattr(scene, "witcher_quick_anim_source", "AUTO") or "AUTO").upper()


def _resolve_quick_anim_source(scene, main_arm_obj) -> str:
    """Resolve which actor_animations CSV to use ('w3' or 'w2').

    AUTO follows the selected armature's source_game; W3/W2 force the override.
    """
    pref = _quick_anim_source_pref(scene)
    if pref == "W3":
        return "w3"
    if pref == "W2":
        return "w2"
    return _source_game_for_armature_obj(main_arm_obj)


def _get_quick_anim_source_key(main_arm_obj, show_all=False, source_override: str = ""):
    if show_all or not main_arm_obj or main_arm_obj.type != "ARMATURE":
        # Show-all still needs to distinguish which CSV we are browsing so the
        # dropdown can swap between the W3 and W2 globals without colliding.
        effective_source = normalize_source_game(source_override or "w3")
        return ("__show_all__", effective_source)
    rig_settings = getattr(main_arm_obj.data, "witcherui_RigSettings", None)
    if rig_settings is None:
        return ("__show_all__", normalize_source_game(source_override or "w3"))
    anim_paths = tuple(
        (set.path, normalize_source_game(getattr(set, "source_game", "") or getattr(rig_settings, "source_game", "w3")))
        for set in rig_settings.animset_list
        if ":" not in set.path
    )
    effective_source = normalize_source_game(source_override) if source_override else _source_game_for_armature_obj(main_arm_obj)
    player_compat = "w2_player" if _uses_w2_player_animsets(main_arm_obj, effective_source) else ""
    return (
        main_arm_obj.name,
        effective_source,
        getattr(rig_settings, "main_entity_skeleton", ""),
        getattr(rig_settings, "main_face_skeleton", ""),
        anim_paths,
        player_compat,
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
    source_override = _resolve_quick_anim_source(context.scene, main_arm_obj)
    fdir_abs = repo_file_for_source(fdir, source_override)
    _mode_map = {'REPLACE': 'replace', 'APPEND': 'append', 'APPEND_AT_CURSOR': 'append_at_cursor'}
    _nla_mode = _mode_map.get(getattr(context.scene, 'witcher_anim_nla_mode', 'REPLACE'), 'replace')
    load_anim_into_scene(context, anim_name, fdir_abs, main_arm_obj, nla_mode=_nla_mode, source_game=source_override)
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

from ..filtered_list.animations_manager import CModStoryBoardAnimationListsManager, CModStoryBoardMimicsListsManager
from ..filtered_list.storyboardasset import CModStoryBoardActor
from .mimic_compat import (
    collect_actor_mimic_animset_paths,
    is_mimic_path_compatible,
    normalize_animset_path,
)

_MIMIC_ANIMSET_NAME_CACHE = {}
_MIMIC_META_INDEX_CACHE = None


def _get_mimic_meta_index():
    global _MIMIC_META_INDEX_CACHE
    meta = CModStoryBoardMimicsListsManager.get_mimics_meta()
    anim_list = list(getattr(meta, "animList", []) or [])
    token = (id(meta), len(anim_list))
    if _MIMIC_META_INDEX_CACHE is not None and _MIMIC_META_INDEX_CACHE[0] == token:
        return _MIMIC_META_INDEX_CACHE[1]

    paths_by_id = {}
    ordered_paths_by_id = {}
    first_path_by_id = {}
    all_paths = set()
    for anim in anim_list:
        anim_id = str(getattr(anim, "id", "") or "").strip().lower()
        anim_path = str(getattr(anim, "path", "") or "").strip()
        norm_path = normalize_animset_path(anim_path)
        if not anim_id or not norm_path:
            continue
        path_set = paths_by_id.setdefault(anim_id, set())
        if norm_path not in path_set:
            path_set.add(norm_path)
            ordered_paths_by_id.setdefault(anim_id, []).append(norm_path)
        first_path_by_id.setdefault(anim_id, anim_path)
        all_paths.add(norm_path)

    index = {
        "paths_by_id": paths_by_id,
        "ordered_paths_by_id": ordered_paths_by_id,
        "first_path_by_id": first_path_by_id,
        "all_paths": all_paths,
    }
    _MIMIC_META_INDEX_CACHE = (token, index)
    return index


def _get_mimic_animation_path_by_name(anim_name, main_arm_obj=None, show_all=False):
    actor_path = _get_actor_mimic_animation_path_by_name(anim_name, main_arm_obj=main_arm_obj, show_all=show_all)
    if actor_path is not None:
        return actor_path

    target = str(anim_name or "").strip().lower()
    if not target:
        return None
    mimic_index = _get_mimic_meta_index()
    first_match = mimic_index["first_path_by_id"].get(target)
    matching_paths = mimic_index["ordered_paths_by_id"].get(target) or ()
    for anim_path in matching_paths:
        if is_mimic_path_compatible(anim_path, main_arm_obj, show_all=show_all):
            return anim_path
    if show_all or main_arm_obj is None:
        return first_match
    return None


def _anim_entry_name(entry):
    return str(getattr(getattr(entry, "animation", None), "name", "") or getattr(entry, "name", "") or "")


def _face_rig_path_for_armature(main_arm_obj):
    if main_arm_obj is None or getattr(main_arm_obj, "type", None) != "ARMATURE":
        return None
    rig_settings = getattr(main_arm_obj.data, "witcherui_RigSettings", None)
    face_skeleton = str(getattr(rig_settings, "main_face_skeleton", "") or "").strip() if rig_settings else ""
    if not face_skeleton:
        face_skeleton = str(main_arm_obj.get("witcher_w2_mimic_float_track_skeleton", "") or "").strip()
    if not face_skeleton:
        face_skeleton = str(main_arm_obj.get("mimicFaceFile", "") or "").strip()
    if not face_skeleton:
        return None
    try:
        return repo_file_for_source(face_skeleton, _source_game_for_armature_obj(main_arm_obj))
    except Exception:
        return None


def _resolve_animset_abs_path(repo_path, source_game=""):
    repo_path = str(repo_path or "").strip()
    if not repo_path:
        return None
    try:
        abs_path = repo_file_for_source(repo_path, source_game)
    except Exception:
        abs_path = ""
    if not abs_path:
        roots = source_roots(bpy.context, source_game)
        if roots:
            abs_path = os.path.join(roots[0], repo_path)
    if not abs_path:
        return None
    if os.path.exists(abs_path + ".json"):
        return abs_path + ".json"
    return abs_path if os.path.exists(abs_path) else None


def _load_mimic_animset_name_lookup(repo_path, main_arm_obj=None):
    norm_path = normalize_animset_path(repo_path)
    abs_path = _resolve_animset_abs_path(repo_path, _source_game_for_armature_obj(main_arm_obj))
    if not abs_path:
        return {}
    try:
        stat = os.stat(abs_path)
        token = (norm_path, abs_path, int(stat.st_mtime), int(stat.st_size))
    except Exception:
        token = (norm_path, abs_path, 0, 0)
    cached = _MIMIC_ANIMSET_NAME_CACHE.get(token)
    if cached is not None:
        return cached

    stale_keys = [key for key in _MIMIC_ANIMSET_NAME_CACHE if key[0] == norm_path and key != token]
    for key in stale_keys:
        _MIMIC_ANIMSET_NAME_CACHE.pop(key, None)

    lookup = {}
    try:
        anim_set = import_anims.import_w3_animSet(abs_path, rigPath=_face_rig_path_for_armature(main_arm_obj))
        for entry in getattr(anim_set, "animations", []) or []:
            name = _anim_entry_name(entry)
            if name:
                lookup.setdefault(name.lower(), name)
    except Exception:
        log.debug("Failed to scan mimic animset '%s' for animation-name fallback.", repo_path, exc_info=True)

    _MIMIC_ANIMSET_NAME_CACHE[token] = lookup
    if len(_MIMIC_ANIMSET_NAME_CACHE) > 64:
        oldest_key = next(iter(_MIMIC_ANIMSET_NAME_CACHE.keys()))
        _MIMIC_ANIMSET_NAME_CACHE.pop(oldest_key, None)
    return lookup


def _get_actor_mimic_animation_path_by_name(anim_name, main_arm_obj=None, show_all=False):
    if show_all or main_arm_obj is None:
        return None
    target = str(anim_name or "").strip().lower()
    if not target:
        return None
    repo_paths = tuple(collect_actor_mimic_animset_paths(main_arm_obj))
    if not repo_paths:
        return None

    mimic_index = _get_mimic_meta_index()
    meta_paths_for_name = mimic_index["paths_by_id"].get(target) or set()
    all_meta_paths = mimic_index["all_paths"]

    # Preserve actor path order, but only binary-scan uncatalogued actor paths.
    # actor_mimics.csv is incomplete for some character-specific mimic layer
    # sets, while scanning known stock animsets for synthetic misses can burn
    # seconds before a later CSV hit.
    for repo_path in repo_paths:
        if repo_path in meta_paths_for_name:
            return repo_path
        if repo_path not in all_meta_paths:
            lookup = _load_mimic_animset_name_lookup(repo_path, main_arm_obj=main_arm_obj)
            if target in lookup:
                return repo_path
    return None


def _normalize_catalog_path(path):
    return str(path or "").replace("/", "\\").strip().lower()


def _actor_compatible_animation_paths(main_arm_obj, source_game=""):
    if main_arm_obj is None or getattr(main_arm_obj, "type", None) != "ARMATURE":
        return set(), set()
    rig_settings = getattr(main_arm_obj.data, "witcherui_RigSettings", None)
    if rig_settings is None:
        return set(), set()

    exact_paths = set()
    normalized_paths = set()
    for animset in getattr(rig_settings, "animset_list", []) or []:
        path = str(getattr(animset, "path", "") or "").strip()
        if not path or ":" in path:
            continue
        exact_paths.add(path)
        normalized_paths.add(_normalize_catalog_path(path))
    if _uses_w2_player_animsets(main_arm_obj, source_game):
        for path in _w2_player_animsets(getattr(bpy, "context", None)):
            exact_paths.add(path)
            normalized_paths.add(_normalize_catalog_path(path))
    return exact_paths, normalized_paths


def _normal_animation_manager(manager=None, source_game: str = "w3"):
    source_game = normalize_source_game(source_game or "w3")
    if manager is None or isinstance(manager, CModStoryBoardMimicsListsManager):
        manager = CModStoryBoardAnimationListsManager()
    loaded_source = normalize_source_game(getattr(manager, "_loadedSourceGame", "") or "")
    if not getattr(manager, "_dataLoaded", False) or loaded_source != source_game:
        manager.lazyLoad(source_game)
    return manager


def _get_actor_compatible_animation_path_by_name(anim_name, main_arm_obj, manager=None, source_game=""):
    source_game = normalize_source_game(source_game) if source_game else _source_game_for_armature_obj(main_arm_obj)
    exact_paths, normalized_paths = _actor_compatible_animation_paths(main_arm_obj, source_game=source_game)
    if not exact_paths and not normalized_paths:
        return None

    try:
        manager = _normal_animation_manager(manager, source_game=source_game)
    except Exception:
        return None

    anim_meta = getattr(manager, "_animMeta", None)
    for anim in getattr(anim_meta, "animList", []) or []:
        if anim.id != anim_name:
            continue
        anim_path = str(getattr(anim, "path", "") or "")
        if anim_path in exact_paths or _normalize_catalog_path(anim_path) in normalized_paths:
            return anim_path
    return None


def GetAnimationInfoByName(anim_name, main_arm_obj=None, show_all=False, prefer_mimic=False, quiet=False,
                           compatible_only=False, source_game=""):
    source_game = normalize_source_game(source_game) if source_game else _source_game_for_armature_obj(main_arm_obj)
    fdir = None
    if prefer_mimic:
        fdir = _get_mimic_animation_path_by_name(anim_name, main_arm_obj=main_arm_obj, show_all=show_all)
        if fdir is None:
            fdir = _get_actor_mimic_animation_path_by_name(anim_name, main_arm_obj=main_arm_obj, show_all=show_all)
    if fdir is None and not prefer_mimic:
        try:
            manager = _normal_animation_manager(CModStoryBoardAnimationListsManager.active, source_game=source_game)
        except Exception:
            manager = None
        actor_aware = main_arm_obj is not None and not show_all
        if actor_aware:
            fdir = _get_actor_compatible_animation_path_by_name(
                anim_name,
                main_arm_obj,
                manager=manager,
                source_game=source_game,
            )
        if fdir is None and manager is not None and getattr(manager, "_animMeta", None) is not None and not (compatible_only and actor_aware):
            active_list = getattr(getattr(manager, "active", manager), "active_list", None)
            active_items = getattr(active_list, "_items", []) or []
            active_slot_ids = {getattr(anim_active, "id", None) for anim_active in active_items}
            for anim in manager._animMeta.animList:
                if anim.id == anim_name:
                    if show_all or not compatible_only or getattr(anim, "slotId", None) in active_slot_ids:
                        fdir = anim.path
                        break
    if fdir is None and not compatible_only:
        fdir = _get_mimic_animation_path_by_name(anim_name, main_arm_obj=main_arm_obj, show_all=show_all)
    if fdir is None:
        if not quiet:
            log.critical('Did not find animation!')
        return (None, None)
    #(, ) = item.animLineId.split(';')
    fdir = repo_file_for_source(fdir, source_game)
    return (anim_name, fdir)

def SetupActor(main_arm_obj, context=None, show_all=False):
    scene = (context.scene if context else bpy.context.scene)
    scene_id = _scene_key(scene)
    show_all = show_all or not main_arm_obj
    source_game = _resolve_quick_anim_source(scene, main_arm_obj)
    source_key = _get_quick_anim_source_key(main_arm_obj, show_all, source_override=source_game)

    animListsManager: CModStoryBoardAnimationListsManager = CModStoryBoardAnimationListsManager()
    actor = CModStoryBoardActor()
    actor.source_game = source_game

    if show_all:
        actor._animPaths = None  # isCompatibleAnimation returns True for all
    else:
        rig_settings = getattr(main_arm_obj.data, "witcherui_RigSettings", None)
        if rig_settings is None:
            log.warning("Armature '%s' has no rig settings; falling back to show-all.", main_arm_obj.name)
            actor._animPaths = None
            source_key = ("__show_all__", source_game)
        else:
            animset_list = rig_settings.animset_list
            actor._animPaths = []
            for set in animset_list:
                if ":" not in set.path:
                    actor._animPaths.append(set.path)
            if _uses_w2_player_animsets(main_arm_obj, source_game):
                seen_paths = {_normalize_catalog_path(path) for path in actor._animPaths}
                for path in _w2_player_animsets(context):
                    norm_path = _normalize_catalog_path(path)
                    if norm_path and norm_path not in seen_paths:
                        actor._animPaths.append(path)
                        seen_paths.add(norm_path)

    animListsManager.lazyLoad(source_game)

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

    source_override = _resolve_quick_anim_source(scene, main_arm_obj)
    source_key = _get_quick_anim_source_key(main_arm_obj, show_all, source_override=source_override)
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

def _is_w2_animation_file(filepath):
    filepath = str(filepath or "")
    if not filepath.lower().endswith((".w2anims", ".w2cutscene")):
        return False
    try:
        return bool(import_anims._is_w2_cr2w_version(filepath))
    except Exception:
        return False


def _resolve_w2_retarget_source_rig(context, target_rig_path="", target_name_hint="", source_anim_path=""):
    from ..CR2W.retarget_anims import infer_w2_source_rig_path

    scene = getattr(context, "scene", None)
    configured = str(getattr(scene, "witcher_w2_retarget_source_rig", "") or "").strip().strip('"')
    candidates = []
    if configured:
        candidates.append(configured)
        if not os.path.isabs(configured):
            try:
                candidates.append(repo_file_for_source(configured, "w2"))
            except Exception:
                pass

    inferred = infer_w2_source_rig_path(target_rig_path, target_name_hint)
    if inferred:
        try:
            resolved_from_source = resolve_w2_repo_file_from_source(inferred, source_anim_path, version=115)
            if resolved_from_source:
                candidates.append(resolved_from_source)
        except Exception:
            pass
        try:
            candidates.append(repo_file_for_source(inferred, "w2"))
        except Exception:
            candidates.append(inferred)

    seen = set()
    for candidate in candidates:
        candidate = str(candidate or "").strip()
        if not candidate:
            continue
        key = os.path.normcase(os.path.normpath(candidate))
        if key in seen:
            continue
        seen.add(key)
        if os.path.exists(candidate):
            return candidate
    return ""


def _should_retarget_w2_to_w3(context, source_game, target_armatures, face_animation):
    if face_animation or normalize_source_game(source_game) != "w2":
        return False
    scene = getattr(context, "scene", None)
    if scene is not None and not bool(getattr(scene, "witcher_w2_retarget_to_w3", True)):
        return False
    target = target_armatures[0] if target_armatures else None
    if target is None or getattr(target, "type", None) != "ARMATURE":
        return False
    return _source_game_for_armature_obj(target) != "w2"


def _retarget_w2_animation_entry(context, animation_entry, source_rig_path, target_rig_path):
    from ..CR2W.dc_skeleton import load_bin_skeleton
    from ..CR2W.retarget_anims import retarget_w2_animation_entry

    if not source_rig_path or not target_rig_path:
        raise RuntimeError("W2 retarget needs both source W2 rig and target W3 rig paths.")
    source_skeleton = load_bin_skeleton(source_rig_path)
    target_skeleton = load_bin_skeleton(target_rig_path)
    in_place = bool(getattr(getattr(context, "scene", None), "witcher_w2_retarget_in_place", False))
    hand_fit = str(getattr(getattr(context, "scene", None), "witcher_w2_retarget_hand_fit", "WEAPON") or "WEAPON")
    return retarget_w2_animation_entry(
        animation_entry,
        source_skeleton,
        target_skeleton,
        in_place=in_place,
        hand_fit=hand_fit,
    )


def load_anim_into_scene(context, anim_name, fdir, main_arm_obj, NLA_track = 'anim_import', at_frame = 0,
                         face_target_mode="auto", nla_mode='replace', target_component="", source_game=""):
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
            target_component=target_component,
        )
    effective_track = NLA_track
    if face_target_mode != "owner" and face_animation and NLA_track == 'anim_import':
        effective_track = 'mimic_import'

    effective_source_game = normalize_source_game(source_game) if source_game else (
        "w2" if _is_w2_animation_file(fdir) else _source_game_for_armature_obj(main_arm_obj)
    )
    retarget_w2 = _should_retarget_w2_to_w3(
        context,
        effective_source_game,
        target_armatures,
        face_animation,
    )
    retarget_source_rig_path = ""
    load_rig_path = rig_path
    if retarget_w2:
        target_hint = _armature_identity_text(target_armatures[0]) if target_armatures else ""
        retarget_source_rig_path = _resolve_w2_retarget_source_rig(
            context,
            target_rig_path=rig_path,
            target_name_hint=target_hint,
            source_anim_path=fdir,
        )
        if not retarget_source_rig_path:
            roots = "; ".join(source_roots(context, "w2")) or "<no configured W2 roots>"
            raise RuntimeError(f"W2 source rig for retarget was not found. Configure W2 Retarget Source Rig. Roots: {roots}")
        load_rig_path = retarget_source_rig_path

    result = load_bin_anims_single(
        fdir,
        anim_name,
        rigPath=load_rig_path,
    )
    if not result or not result.animations:
        raise RuntimeError(f"Animation '{anim_name}' was not found in {fdir}")
    animation = result.animations[0]
    if retarget_w2:
        animation = _retarget_w2_animation_entry(
            context,
            animation,
            retarget_source_rig_path,
            rig_path,
        )

    try:
        anim_data = getattr(animation, "animation", None)
        scene = getattr(context, "scene", None)
        if anim_data is not None and scene is not None:
            scene["_w3_last_anim_name"] = str(getattr(anim_data, "name", anim_name) or anim_name)
            scene["_w3_last_anim_type"] = str(getattr(anim_data, "SkeletalAnimationType", "") or "")
            scene["_w3_last_anim_additive"] = str(getattr(anim_data, "AdditiveType", "") or "")
            scene["_w3_last_anim_fps"] = float(getattr(anim_data, "framesPerSecond", 0.0) or 0.0)
            scene["_w3_last_anim_duration"] = float(getattr(anim_data, "duration", 0.0) or 0.0)
            scene["_w3_last_anim_path"] = str(fdir or "")
            scene["_w3_last_anim_source_game"] = effective_source_game
            scene["_w3_last_anim_target_component"] = str(target_component or "")
    except Exception:
        pass

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

    effective_at_frame = float(context.scene.frame_current) if nla_mode == 'append_at_cursor' else at_frame
    import_anims.import_anim(
        context,
        fdir,
        animation,
        use_NLA=True,
        NLA_track=effective_track,
        override_select=actual_target_armatures if len(actual_target_armatures) > 1 else actual_target_armatures[0],
        at_frame=effective_at_frame,
        nla_mode=nla_mode,
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


class WITCH_OT_QuickDialogsetBodyResolve(bpy.types.Operator):
    bl_idname = "witcher.quick_dialogset_body_resolve"
    bl_label = "Resolve Dialogset Body"
    bl_description = "Resolve the selected dialogset body preset without importing it"
    bl_options = {'REGISTER'}

    def execute(self, context):
        try:
            actor_obj, info = resolve_and_store_quick_dialogset_body(context)
            if actor_obj is None:
                self.report({'WARNING'}, "No target character selected.")
                return {'CANCELLED'}
            if not info.get("anim_id"):
                self.report({'WARNING'}, "No body animation found for this combination.")
                return {'CANCELLED'}
            if not info.get("resolved_anim_name"):
                self.report({'WARNING'}, "Body animation is not compatible with the current character's animation sets.")
                return {'CANCELLED'}
        except Exception as exc:
            log.error("Dialogset body resolve failed.", exc_info=True)
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class WITCH_OT_QuickDialogsetBodyLoad(bpy.types.Operator):
    bl_idname = "witcher.quick_dialogset_body_load"
    bl_label = "Load Dialogset Body"
    bl_description = "Load the resolved dialogset body animation onto the current character"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        try:
            if not load_quick_dialogset_body(context):
                self.report({'WARNING'}, "No compatible dialogset body animation was loaded.")
                return {'CANCELLED'}
        except Exception as exc:
            log.error("Dialogset body load failed.", exc_info=True)
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
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
    source_override = _resolve_quick_anim_source(context.scene, main_arm_obj)
    source_key = _get_quick_anim_source_key(main_arm_obj, source_override=source_override)
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
        WITCH_OT_QuickDialogsetBodyResolve,
        WITCH_OT_QuickDialogsetBodyLoad,
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
    if not hasattr(bpy.types.Scene, "witcher_quick_anim_source"):
        bpy.types.Scene.witcher_quick_anim_source = EnumProperty(
            name="Source",
            description="Which game's animation catalog the Quick Animation Browser draws from. AUTO follows the selected character's source game.",
            items=[
                ("AUTO", "Auto", "Pick W3 or W2 based on the selected character's source game"),
                ("W3", "W3", "Force the Witcher 3 actor_animations catalog"),
                ("W2", "W2", "Force the Witcher 2 actor_animations catalog"),
            ],
            default="AUTO",
            update=lambda self, ctx: _schedule_deferred_quick_anim_setup(),
        )
    if not hasattr(bpy.types.Scene, DIALOGSET_BODY_STATUS_PROP):
        setattr(bpy.types.Scene, DIALOGSET_BODY_STATUS_PROP, EnumProperty(
            name="actorStatus",
            description="Actor status from scene_body_animations.csv",
            items=_dialogset_body_status_items,
            update=on_dialogset_body_status_changed,
        ))
    if not hasattr(bpy.types.Scene, DIALOGSET_BODY_EMOTIONAL_PROP):
        setattr(bpy.types.Scene, DIALOGSET_BODY_EMOTIONAL_PROP, EnumProperty(
            name="actorEmotionalState",
            description="Actor emotional state filtered by actorStatus",
            items=_dialogset_body_emotional_items,
            update=on_dialogset_body_emotional_changed,
        ))
    if not hasattr(bpy.types.Scene, DIALOGSET_BODY_POSE_PROP):
        setattr(bpy.types.Scene, DIALOGSET_BODY_POSE_PROP, EnumProperty(
            name="actorPoseName",
            description="Actor body pose filtered by actorStatus and actorEmotionalState",
            items=_dialogset_body_pose_items,
            update=on_dialogset_body_pose_changed,
        ))
    for prop_name in (
        DIALOGSET_BODY_RESOLVED_STATUS_PROP,
        DIALOGSET_BODY_RESOLVED_ANIM_PROP,
    ):
        if not hasattr(bpy.types.Scene, prop_name):
            setattr(bpy.types.Scene, prop_name, StringProperty(default=""))

def unregister():
    global _DIALOGSET_BODY_ITEMS_CACHE
    _DIALOGSET_BODY_ITEMS_CACHE = None
    _DIALOGSET_BODY_RESOLVE_CACHE.clear()
    _QUICK_ANIM_FILTER_CACHE.clear()
    _ACTIVE_SOURCE_KEY_BY_SCENE.clear()
    _LAST_QUICK_ANIM_SEARCH_BY_SCENE.clear()
    for prop_name in (
        DIALOGSET_BODY_RESOLVED_ANIM_PROP,
        DIALOGSET_BODY_RESOLVED_STATUS_PROP,
        DIALOGSET_BODY_POSE_PROP,
        DIALOGSET_BODY_EMOTIONAL_PROP,
        DIALOGSET_BODY_STATUS_PROP,
    ):
        if hasattr(bpy.types.Scene, prop_name):
            delattr(bpy.types.Scene, prop_name)
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
    if hasattr(bpy.types.Scene, "witcher_quick_anim_source"):
        del bpy.types.Scene.witcher_quick_anim_source
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()

