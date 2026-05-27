import json
import logging
import os

import bpy


log = logging.getLogger(__name__)


_RIG_MIMIC_PATH_CACHE = {}


def normalize_animset_path(path):
    text = str(path or "").strip().replace("/", "\\")
    if text.lower().endswith(".json"):
        text = text[:-5]
    return text.lower()


def _looks_like_mimic_animset(path):
    norm = normalize_animset_path(path)
    if not norm.endswith(".w2anims"):
        return False
    # Witcher 2 support: face/lipsync sets are not always under a "mimics"
    # folder. Some inherited/common clips live in templates\face or use
    # lipsync-style names while still driving the W2 float-track face rig.
    base = norm.rsplit("\\", 1)[-1]
    return (
        "\\mimics\\" in norm
        or "\\templates\\face\\" in norm
        or "mimic" in base
        or "lipsync" in norm
        or base == "mimika.w2anims"
    )


def _extract_animset_path(candidate):
    if candidate is None:
        return ""
    if isinstance(candidate, str):
        return candidate
    if isinstance(candidate, dict):
        return (
            candidate.get("path")
            or candidate.get("DepotPath")
            or candidate.get("depotPath")
            or candidate.get("_depotPath")
            or candidate.get("_value")
            or ""
        )
    return (
        getattr(candidate, "path", None)
        or getattr(candidate, "DepotPath", None)
        or getattr(candidate, "depotPath", None)
        or ""
    )


def _iter_animset_paths(value):
    if value is None:
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _iter_animset_paths(item)
        return
    if isinstance(value, dict):
        if "animationSets" in value:
            yield from _iter_animset_paths(value.get("animationSets"))
            return
        path = _extract_animset_path(value)
        if path:
            yield path
        return
    path = _extract_animset_path(value)
    if path:
        yield path


def _dedupe_normalized_paths(paths):
    out = []
    seen = set()
    for path in paths or []:
        norm = normalize_animset_path(path)
        if not norm or not norm.endswith(".w2anims") or norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
    return tuple(out)


def _iter_mimic_paths_from_json_data(json_text):
    if not json_text:
        return
    try:
        data = json.loads(json_text)
    except Exception:
        log.debug("Could not parse entity jsonData while resolving mimic sets.", exc_info=True)
        return
    if isinstance(data, dict):
        yield from _iter_animset_paths(data.get("CAnimMimicParam"))


def _iter_mimic_paths_from_animset_list(rig_settings):
    in_mimic_group = False
    for item in getattr(rig_settings, "animset_list", []) or []:
        path = str(getattr(item, "path", "") or "").strip()
        name = str(getattr(item, "name", "") or "").strip()
        if ":" in path:
            group_text = f"{path} {name}".lower()
            in_mimic_group = "mimic" in group_text
            continue
        if in_mimic_group or _looks_like_mimic_animset(path):
            yield path


def _rig_cache_token(rig_settings):
    if rig_settings is None:
        return None
    id_data = getattr(rig_settings, "id_data", None)
    try:
        id_key = int(id_data.as_pointer())
    except Exception:
        id_key = id(id_data)
    json_text = str(getattr(rig_settings, "jsonData", "") or "")
    animset_paths = tuple(str(getattr(item, "path", "") or "") for item in getattr(rig_settings, "animset_list", []) or [])
    return (
        id_key,
        len(json_text),
        json_text[:128],
        json_text[-128:] if json_text else "",
        animset_paths,
    )


def _collect_mimic_paths_from_rig_settings(rig_settings):
    token = _rig_cache_token(rig_settings)
    if token is None:
        return ()
    cached = _RIG_MIMIC_PATH_CACHE.get(token)
    if cached is not None:
        return cached

    paths = []
    paths.extend(_iter_mimic_paths_from_animset_list(rig_settings))
    paths.extend(_iter_mimic_paths_from_json_data(str(getattr(rig_settings, "jsonData", "") or "")))
    result = _dedupe_normalized_paths(paths)
    _RIG_MIMIC_PATH_CACHE[token] = result

    if len(_RIG_MIMIC_PATH_CACHE) > 128:
        oldest_key = next(iter(_RIG_MIMIC_PATH_CACHE.keys()))
        _RIG_MIMIC_PATH_CACHE.pop(oldest_key, None)
    return result


def _is_armature(obj):
    return bool(obj and getattr(obj, "type", None) == "ARMATURE")


def _is_mimic_component_armature(obj):
    if not _is_armature(obj):
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
    if bool(obj.get("witcher_w2_mimic_support", False)):
        w2_mimic_armature = str(obj.get("witcher_w2_mimic_armature", "") or "").strip()
        if w2_mimic_armature and w2_mimic_armature == object_name:
            return True
    return False


def _iter_descendant_armatures(root_obj):
    pending = list(getattr(root_obj, "children", []) or [])
    while pending:
        child = pending.pop(0)
        pending.extend(getattr(child, "children", []) or [])
        if _is_armature(child):
            yield child


def _find_named_mimic_armature(root_obj):
    mimic_name = str(root_obj.get("mimicFace", "") or "").strip() if root_obj else ""
    if not mimic_name and root_obj:
        mimic_name = str(root_obj.get("witcher_w2_mimic_armature", "") or "").strip()
    if not mimic_name:
        return None
    candidate = bpy.data.objects.get(mimic_name)
    return candidate if _is_mimic_component_armature(candidate) else None


def _iter_owner_armatures_for_mimic(mimic_arm_obj):
    if not _is_mimic_component_armature(mimic_arm_obj):
        return
    mimic_name = str(getattr(mimic_arm_obj, "name", "") or "").strip()
    if not mimic_name:
        return
    scene = getattr(bpy.context, "scene", None)
    for obj in getattr(scene, "objects", []) if scene is not None else []:
        if obj is mimic_arm_obj or not _is_armature(obj) or _is_mimic_component_armature(obj):
            continue
        if str(obj.get("mimicFace", "") or "").strip() == mimic_name:
            yield obj


def _unique_armatures(armatures):
    out = []
    seen = set()
    for obj in armatures or []:
        if not _is_armature(obj):
            continue
        name = str(getattr(obj, "name", "") or "")
        if name in seen:
            continue
        seen.add(name)
        out.append(obj)
    return out


def _iter_mimic_path_armature_candidates(armature_obj):
    if not _is_armature(armature_obj):
        return []

    candidates = [armature_obj]
    if _is_mimic_component_armature(armature_obj):
        candidates.extend(_iter_owner_armatures_for_mimic(armature_obj))
    else:
        named_mimic = _find_named_mimic_armature(armature_obj)
        if named_mimic is not None:
            candidates.append(named_mimic)
        candidates.extend(_iter_descendant_armatures(armature_obj))
    return _unique_armatures(candidates)


def collect_actor_mimic_animset_paths(armature_obj):
    paths = []
    for candidate in _iter_mimic_path_armature_candidates(armature_obj):
        rig_settings = getattr(getattr(candidate, "data", None), "witcherui_RigSettings", None)
        paths.extend(_collect_mimic_paths_from_rig_settings(rig_settings))
    return _dedupe_normalized_paths(paths)


def is_mimic_path_compatible(path, armature_obj, show_all=False):
    if show_all or armature_obj is None:
        return True
    compatible_paths = set(collect_actor_mimic_animset_paths(armature_obj))
    if not compatible_paths:
        return False
    return normalize_animset_path(path) in compatible_paths


def get_quick_mimic_source_key(armature_obj, show_all=False):
    if show_all or armature_obj is None or not _is_armature(armature_obj):
        return ("__show_all__",)
    rig_settings = getattr(getattr(armature_obj, "data", None), "witcherui_RigSettings", None)
    return (
        getattr(armature_obj, "name", ""),
        str(getattr(rig_settings, "main_face_skeleton", "") or "") if rig_settings else "",
        str(getattr(rig_settings, "entity_name", "") or "") if rig_settings else "",
        collect_actor_mimic_animset_paths(armature_obj),
    )
