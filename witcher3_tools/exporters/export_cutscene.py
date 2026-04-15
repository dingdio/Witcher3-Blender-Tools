import collections
import logging
import math
import os
import re
import bpy
from typing import Dict, List

from ..CR2W import anims_builder, cr2w_writer
from ..action_compat import resolve_action_slot
from ..external_addon_tools import get_re_addon_status
from . import export_anims


log = logging.getLogger(__name__)


CUTSCENE_TRACK_NAME = "cutscene_import"
CUTSCENE_FACE_TRACK_NAME = f"{CUTSCENE_TRACK_NAME}_face"
CUTSCENE_SOURCE_PATH_PROP = "witcher_cutscene_source_path"
CUTSCENE_SOURCE_INDEX_PROP = "witcher_cutscene_source_index"
CUTSCENE_ANIMATION_NAME_PROP = "witcher_cutscene_animation_name"
CUTSCENE_RE_EXPORT_SUFFIX = "_redkit"
_VALID_CUTSCENE_ACTOR_TYPES = ("CAT_None", "CAT_Actor", "CAT_Prop", "CAT_Camera")
_INVALID_PATH_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_BLENDER_DUPLICATE_SUFFIX_RE = re.compile(r"\.\d{3}$")

CUTSCENE_POINT_TAGS_PROP = "witcher_cutscene_point_tags"
CUTSCENE_LAST_LEVEL_LOADED_PROP = "witcher_cutscene_last_level_loaded"
CUTSCENE_USED_IN_FILES_PROP = "witcher_cutscene_used_in_files"
CUTSCENE_EXPORT_METADATA_SYNCED_PROP = "witcher_cutscene_export_metadata_synced"


def _split_metadata_text_list(value: str) -> List[str]:
    items = []
    for item in str(value or "").split(";"):
        item_text = export_anims._strip_text(item)
        if item_text:
            items.append(item_text)
    return items


def _scene_cutscene_template_metadata(scene) -> Dict[str, object]:
    return {
        "point": _split_metadata_text_list(getattr(scene, CUTSCENE_POINT_TAGS_PROP, "")),
        "lastLevelLoaded": export_anims._strip_text(getattr(scene, CUTSCENE_LAST_LEVEL_LOADED_PROP, "")),
        "usedInFiles": _split_metadata_text_list(getattr(scene, CUTSCENE_USED_IN_FILES_PROP, "")),
        "burnedAudioTrackName": export_anims._strip_text(
            getattr(scene, "witcher_cutscene_burned_audio_event", "")
        ),
        "_synced": bool(getattr(scene, CUTSCENE_EXPORT_METADATA_SYNCED_PROP, False)),
    }


def _source_cutscene_template_metadata(source_path: str, source_cache: Dict[str, object]) -> Dict[str, object]:
    cutscene_template = _load_cutscene_source_template(source_path, source_cache)
    if cutscene_template is None:
        return {}
    return {
        "point": [
            export_anims._strip_text(value)
            for value in (getattr(cutscene_template, "point", None) or [])
            if export_anims._strip_text(value)
        ],
        "lastLevelLoaded": export_anims._strip_text(getattr(cutscene_template, "lastLevelLoaded", "")),
        "usedInFiles": [
            export_anims._strip_text(value)
            for value in (getattr(cutscene_template, "usedInFiles", None) or [])
            if export_anims._strip_text(value)
        ],
        "burnedAudioTrackName": export_anims._strip_text(
            getattr(cutscene_template, "burnedAudioTrackName", "")
        ),
    }


def _collect_cutscene_template_metadata(scene, export_entries, source_cache: Dict[str, object]) -> Dict[str, object]:
    scene_metadata = _scene_cutscene_template_metadata(scene)
    synced = scene_metadata.pop("_synced", False)

    candidate_paths = []
    seen_paths = set()

    loaded_path = export_anims._strip_text(getattr(scene, "witcher_loaded_w2cutscene_path", ""))
    if loaded_path:
        candidate_paths.append(loaded_path)
        seen_paths.add(os.path.normcase(os.path.normpath(loaded_path)))

    for entry in export_entries:
        source_path = export_anims._strip_text(entry.get("source_path", ""))
        if not source_path:
            continue
        norm_path = os.path.normcase(os.path.normpath(source_path))
        if norm_path in seen_paths:
            continue
        seen_paths.add(norm_path)
        candidate_paths.append(source_path)

    merged = dict(scene_metadata) if synced else {}
    fields = ("point", "lastLevelLoaded", "usedInFiles", "burnedAudioTrackName")

    def _is_empty(value):
        if value is None:
            return True
        if isinstance(value, (list, tuple, set, dict)):
            return len(value) == 0
        return not str(value).strip()

    missing = [field for field in fields if _is_empty(merged.get(field))]
    if missing:
        for source_path in candidate_paths:
            source_metadata = _source_cutscene_template_metadata(source_path, source_cache)
            if not source_metadata:
                continue
            for field in list(missing):
                value = source_metadata.get(field)
                if not _is_empty(value):
                    merged[field] = value
                    missing.remove(field)
            if not missing:
                break

    if not merged:
        merged = dict(scene_metadata)
    return merged


def _resolve_filesystem_export_path(filepath: str) -> str:
    path = bpy.path.abspath(filepath or "")
    if path.startswith("//"):
        path = os.path.abspath(path.replace("//", ""))
    return os.path.normpath(path)


def _sanitize_cutscene_path_part(value: str, fallback: str = "item") -> str:
    text = _INVALID_PATH_CHARS_RE.sub("_", export_anims._strip_text(value))
    text = text.strip(" ._")
    return text or fallback


def _normalize_cutscene_actor_type(actor_type, actor_name: str = "") -> str:
    actor_type_text = export_anims._strip_text(actor_type)
    for candidate in _VALID_CUTSCENE_ACTOR_TYPES:
        if actor_type_text == candidate or candidate in actor_type_text:
            return candidate
    if export_anims._strip_text(actor_name).lower() == "camera":
        return "CAT_Camera"
    return "CAT_Actor"


def _split_cutscene_animation_name(anim_name: str):
    full_name = export_anims._strip_text(anim_name)
    parts = full_name.split(":", 2)
    if len(parts) >= 3:
        actor_name, component_name, display_name = parts
    elif len(parts) == 2:
        actor_name, display_name = parts
        component_name = ""
    else:
        actor_name = ""
        component_name = ""
        display_name = full_name
    return actor_name, component_name, display_name


def _strip_blender_duplicate_suffix(value: str) -> str:
    return _BLENDER_DUPLICATE_SUFFIX_RE.sub("", export_anims._strip_text(value))


def _is_cutscene_track_name(track_name: str) -> bool:
    text = export_anims._strip_text(track_name)
    return text == CUTSCENE_TRACK_NAME or text.startswith(CUTSCENE_TRACK_NAME)


def _iter_scene_armatures(scene=None):
    scene = scene or getattr(bpy.context, "scene", None)
    if scene is None:
        return
    for obj in scene.objects:
        if getattr(obj, "type", None) == 'ARMATURE':
            yield obj


def _iter_object_descendants(root_obj):
    pending = list(getattr(root_obj, "children", []) or [])
    while pending:
        child = pending.pop(0)
        pending.extend(getattr(child, "children", []) or [])
        yield child


def _iter_additional_cutscene_armatures(actor_obj, scene=None):
    if actor_obj is None or getattr(actor_obj, "type", None) != 'ARMATURE':
        return
    actor_name = export_anims._strip_text(actor_obj.get("cutscene_actor_name", ""))
    if not actor_name:
        return
    for obj in _iter_scene_armatures(scene):
        if obj is actor_obj:
            continue
        if export_anims._strip_text(obj.get("cutscene_actor_name", "")) == actor_name:
            yield obj


def _iter_cutscene_related_armatures(actor_obj, scene=None):
    seen = set()

    def _yield_once(obj):
        if obj is None or getattr(obj, "type", None) != 'ARMATURE':
            return
        obj_name = export_anims._strip_text(getattr(obj, "name", ""))
        if not obj_name or obj_name in seen:
            return
        seen.add(obj_name)
        yield obj

    if actor_obj is not None:
        yield from _yield_once(actor_obj)
    for child in _iter_object_descendants(actor_obj):
        yield from _yield_once(child)
    for extra_obj in _iter_additional_cutscene_armatures(actor_obj, scene):
        yield from _yield_once(extra_obj)


def _resolve_cutscene_skeleton_path(armature_obj, component, scene=None) -> str:
    if armature_obj is None:
        return ""

    # In w2cutscene files, ALL animations (including face/mimic) reference the body
    # skeleton (.w2rig), not the face rig (.w3fac). The face rig is part of the entity,
    # not the cutscene. Using the face skeleton here would add a .w3fac import to the
    # cutscene file, which REDkit cannot cast to CSkeleton and will crash on load.
    for candidate in _iter_cutscene_related_armatures(armature_obj, scene):
        entity_skeleton, _face_skeleton = export_anims._get_armature_skeleton_paths(candidate)
        if entity_skeleton:
            return entity_skeleton

    return ""


def _object_depth(obj) -> int:
    depth = 0
    current = getattr(obj, "parent", None)
    while current is not None:
        depth += 1
        current = getattr(current, "parent", None)
    return depth


def _cutscene_actor_sort_key(actor_obj):
    source_index = export_anims._safe_int(actor_obj.get(CUTSCENE_SOURCE_INDEX_PROP, -1), -1)
    missing_index = 1 if source_index < 0 else 0
    return (
        missing_index,
        source_index if source_index >= 0 else 0,
        _object_depth(actor_obj),
        export_anims._strip_text(actor_obj.get("cutscene_actor_name", "")),
        export_anims._strip_text(getattr(actor_obj, "name", "")),
    )


def _collect_cutscene_actor_roots(scene=None):
    grouped = collections.defaultdict(list)
    for obj in _iter_scene_armatures(scene):
        actor_name = export_anims._strip_text(obj.get("cutscene_actor_name", ""))
        if not actor_name:
            continue
        grouped[actor_name].append(obj)

    actor_roots = []
    for actor_name in sorted(grouped.keys()):
        objs = grouped[actor_name]
        actor_roots.append(sorted(objs, key=_cutscene_actor_sort_key)[0])
    actor_roots.sort(key=_cutscene_actor_sort_key)
    return actor_roots


def _resolve_action_frame_range(action, strip=None):
    if action is None:
        return 0, 0

    start = getattr(strip, "action_frame_start", None) if strip is not None else None
    end = getattr(strip, "action_frame_end", None) if strip is not None else None
    if start is None or end is None:
        start, end = getattr(action, "frame_range", (0.0, 0.0))

    start = int(math.floor(float(start) + 1e-6))
    end = int(math.ceil(float(end) - 1e-6))
    if end < start:
        end = start
    return start, end


def _scene_fps(scene=None) -> float:
    scene = scene or getattr(bpy.context, "scene", None)
    render = getattr(scene, "render", None)
    fps = float(getattr(render, "fps", export_anims.CUTSCENE_DEFAULT_FPS) or export_anims.CUTSCENE_DEFAULT_FPS)
    fps_base = float(getattr(render, "fps_base", 1.0) or 1.0)
    if fps_base <= 0.0:
        fps_base = 1.0
    fps = fps / fps_base
    return fps if fps > 0.0 else export_anims.CUTSCENE_DEFAULT_FPS


def _collect_cutscene_scene_actors(scene=None):
    actors = []
    for actor_obj in _collect_cutscene_actor_roots(scene):
        actor_name = export_anims._strip_text(actor_obj.get("cutscene_actor_name", ""))
        if not actor_name:
            continue
        actors.append({
            "name": actor_name,
            "template": export_anims._strip_text(actor_obj.get("cutscene_actor_template", "")),
            "appearance": export_anims._strip_text(actor_obj.get("cutscene_actor_appearance", "")),
            "type": _normalize_cutscene_actor_type(
                actor_obj.get("cutscene_actor_type", ""),
                actor_name=actor_name,
            ),
            "use_mimic": bool(actor_obj.get("cutscene_actor_use_mimic", False)),
            "source_index": export_anims._safe_int(actor_obj.get(CUTSCENE_SOURCE_INDEX_PROP, -1), -1),
        })
    actors.sort(key=lambda actor: (
        1 if int(actor.get("source_index", -1) or -1) < 0 else 0,
        int(actor.get("source_index", -1) or -1) if int(actor.get("source_index", -1) or -1) >= 0 else 0,
        export_anims._strip_text(actor.get("name", "")),
    ))
    return actors


def _cutscene_entry_sort_key(entry):
    source_index = int(entry.get("source_index", -1) or -1)
    return (
        1 if source_index < 0 else 0,
        source_index if source_index >= 0 else 0,
        float(entry.get("strip_frame_start", 0.0) or 0.0),
        export_anims._strip_text(entry.get("actor_name", "")),
        export_anims._strip_text(entry.get("component", "")),
        export_anims._strip_text(entry.get("armature_name", "")),
        export_anims._strip_text(entry.get("strip_name", "")),
    )


def _collect_cutscene_nla_entries(context):
    scene = getattr(context, "scene", None) or getattr(bpy.context, "scene", None)
    entries = []
    for actor_root in _collect_cutscene_actor_roots(scene):
        default_actor_name = export_anims._strip_text(actor_root.get("cutscene_actor_name", ""))
        for armature_obj in _iter_cutscene_related_armatures(actor_root, scene):
            anim_data = getattr(armature_obj, "animation_data", None)
            if not anim_data:
                continue
            for track in getattr(anim_data, "nla_tracks", []) or []:
                track_name = export_anims._strip_text(getattr(track, "name", ""))
                if not _is_cutscene_track_name(track_name) or getattr(track, "mute", False):
                    continue
                for strip in track.strips:
                    if getattr(strip, "mute", False):
                        continue
                    action = getattr(strip, "action", None)
                    if action is None:
                        continue

                    stored_anim_name = export_anims._strip_text(action.get(CUTSCENE_ANIMATION_NAME_PROP, ""))
                    anim_label = (
                        stored_anim_name
                        or export_anims._strip_text(getattr(strip, "name", ""))
                        or export_anims._strip_text(getattr(action, "name", ""))
                    )
                    parsed_actor_name, component_name, display_name = _split_cutscene_animation_name(anim_label)
                    actor_name = parsed_actor_name or default_actor_name
                    if not actor_name:
                        continue
                    if not component_name:
                        if track_name == CUTSCENE_FACE_TRACK_NAME or ":face" in anim_label.lower():
                            component_name = "face"
                        else:
                            component_name = export_anims._strip_text(armature_obj.get("cutscene_component", "")) or "Root"
                    action_name = _strip_blender_duplicate_suffix(
                        display_name
                        or export_anims._strip_text(getattr(action, "name", ""))
                        or anim_label
                        or component_name
                    )
                    frame_start, frame_end = _resolve_action_frame_range(action, strip=strip)
                    entries.append({
                        "actor_name": actor_name,
                        "component": component_name,
                        "action_name": action_name,
                        "source_animation_name": stored_anim_name or export_anims._compose_cutscene_animation_name(actor_name, component_name, action_name),
                        "action": action,
                        "armature_obj": armature_obj,
                        "armature_name": export_anims._strip_text(getattr(armature_obj, "name", "")),
                        "track_name": track_name,
                        "strip_name": export_anims._strip_text(getattr(strip, "name", "")),
                        "strip_frame_start": float(getattr(strip, "frame_start", 0.0) or 0.0),
                        "source_path": export_anims._strip_text(action.get(CUTSCENE_SOURCE_PATH_PROP, "")),
                        "source_index": export_anims._safe_int(action.get(CUTSCENE_SOURCE_INDEX_PROP, -1), -1),
                        "frame_start": frame_start,
                        "frame_end": frame_end,
                    })
    return sorted(entries, key=_cutscene_entry_sort_key)


def _collect_cutscene_active_entries(context):
    scene = getattr(context, "scene", None) or getattr(bpy.context, "scene", None)
    entries = []
    for actor_obj in _collect_cutscene_actor_roots(scene):
        actor_name = export_anims._strip_text(actor_obj.get("cutscene_actor_name", ""))
        if not actor_name:
            continue
        anim_data = getattr(actor_obj, "animation_data", None)
        action = getattr(anim_data, "action", None) if anim_data else None
        if action is None:
            log.warning('Armature "%s" has no active action, skipping fallback cutscene export', actor_obj.name)
            continue
        frame_start, frame_end = _resolve_action_frame_range(action)
        entries.append({
            "actor_name": actor_name,
            "component": export_anims._strip_text(actor_obj.get("cutscene_component", "")) or "Root",
            "action_name": _strip_blender_duplicate_suffix(
                export_anims._strip_text(getattr(action, "name", "")) or actor_name
            ),
            "source_animation_name": export_anims._strip_text(action.get(CUTSCENE_ANIMATION_NAME_PROP, "")),
            "action": action,
            "armature_obj": actor_obj,
            "armature_name": export_anims._strip_text(getattr(actor_obj, "name", "")),
            "track_name": "",
            "strip_name": "",
            "strip_frame_start": float(frame_start),
            "source_path": "",
            "source_index": -1,
            "frame_start": frame_start,
            "frame_end": frame_end,
    })
    return entries


def _cutscene_group_key(entry):
    source_path = export_anims._strip_text(entry.get("source_path", ""))
    source_index = export_anims._safe_int(entry.get("source_index", -1), -1)
    source_anim_name = _strip_blender_duplicate_suffix(entry.get("source_animation_name", ""))
    return (
        export_anims._strip_text(entry.get("actor_name", "")),
        export_anims._normalize_cutscene_component(entry.get("component", "")),
        source_path,
        source_index,
        source_anim_name,
    )


def _resolve_cutscene_group_action_name(entries) -> str:
    for entry in entries:
        source_animation_name = export_anims._strip_text(entry.get("source_animation_name", ""))
        if not source_animation_name:
            continue
        _actor_name, _component_name, display_name = _split_cutscene_animation_name(source_animation_name)
        candidate = _strip_blender_duplicate_suffix(display_name or source_animation_name)
        if candidate:
            return candidate

    for entry in entries:
        for candidate in (
            entry.get("action_name", ""),
            entry.get("strip_name", ""),
            getattr(entry.get("action"), "name", "") if entry.get("action") is not None else "",
        ):
            candidate = _strip_blender_duplicate_suffix(candidate)
            if candidate:
                return candidate

    return "cutscene"


def _group_cutscene_entries(export_entries):
    grouped_entries = collections.OrderedDict()
    for entry in sorted(export_entries, key=_cutscene_entry_sort_key):
        group_key = _cutscene_group_key(entry)
        grouped_entries.setdefault(group_key, []).append(entry)

    groups = []
    for entries in grouped_entries.values():
        ordered_entries = sorted(
            entries,
            key=lambda entry: (
                float(entry.get("strip_frame_start", 0.0) or 0.0),
                int(entry.get("frame_start", 0) or 0),
                int(entry.get("frame_end", 0) or 0),
                export_anims._strip_text(entry.get("strip_name", "")),
            ),
        )
        if not ordered_entries:
            continue

        base_strip_start = min(float(entry.get("strip_frame_start", 0.0) or 0.0) for entry in ordered_entries)
        action_name = _resolve_cutscene_group_action_name(ordered_entries)
        actor_name = export_anims._strip_text(ordered_entries[0].get("actor_name", ""))
        component = export_anims._normalize_cutscene_component(ordered_entries[0].get("component", ""))

        parts = []
        total_num_frames = 0
        for part_index, entry in enumerate(ordered_entries):
            part_num_frames = max(1, int(entry.get("frame_end", 0) or 0) - int(entry.get("frame_start", 0) or 0) + 1)
            part_start_frame = max(
                0,
                int(round(float(entry.get("strip_frame_start", 0.0) or 0.0) - base_strip_start)),
            )
            total_num_frames = max(total_num_frames, part_start_frame + part_num_frames)
            part_entry = dict(entry)
            part_entry["part_index"] = part_index
            part_entry["part_start_frame"] = part_start_frame
            part_entry["part_num_frames"] = part_num_frames
            part_entry["action_name"] = action_name
            part_entry["source_animation_name"] = export_anims._compose_cutscene_animation_name(
                actor_name,
                component,
                action_name,
            )
            parts.append(part_entry)

        primary_entry = parts[0]
        groups.append({
            "actor_name": actor_name,
            "component": component,
            "action_name": action_name,
            "armature_obj": primary_entry.get("armature_obj"),
            "armature_name": export_anims._strip_text(primary_entry.get("armature_name", "")),
            "track_name": export_anims._strip_text(primary_entry.get("track_name", "")),
            "fps": float(primary_entry.get("fps", export_anims.CUTSCENE_DEFAULT_FPS) or export_anims.CUTSCENE_DEFAULT_FPS),
            "source_path": export_anims._strip_text(primary_entry.get("source_path", "")),
            "source_index": export_anims._safe_int(primary_entry.get("source_index", -1), -1),
            "source_animation_name": export_anims._compose_cutscene_animation_name(actor_name, component, action_name),
            "strip_frame_start": base_strip_start,
            "num_frames": max(1, total_num_frames),
            "parts": parts,
            "entries": parts,
        })
    return groups


def _load_cutscene_source_template(source_path: str, source_cache: Dict[str, object]):
    source_path = export_anims._strip_text(source_path)
    if not source_path or not source_path.lower().endswith(".w2cutscene"):
        return None
    if source_path in source_cache:
        return source_cache[source_path]
    cutscene_template = None
    try:
        from ..CR2W.dc_anims import load_bin_cutscene

        cutscene_template = load_bin_cutscene(source_path)
    except Exception:
        log.warning("Failed to inspect source cutscene '%s' while exporting.", source_path, exc_info=True)
    source_cache[source_path] = cutscene_template
    return cutscene_template


def _resolve_cutscene_entry_fps(entry, scene, source_cache) -> float:
    source_path = export_anims._strip_text(entry.get("source_path", ""))
    source_index = int(entry.get("source_index", -1) or -1)
    if source_path and source_index >= 0:
        cutscene_template = _load_cutscene_source_template(source_path, source_cache)
        animations = getattr(cutscene_template, "animations", None) or []
        if 0 <= source_index < len(animations):
            animation = getattr(animations[source_index], "animation", None)
            fps = float(getattr(animation, "framesPerSecond", 0.0) or 0.0)
            if fps > 0.0:
                return fps
    return _scene_fps(scene)


def _extract_handle_depot_path(handle_like) -> str:
    if handle_like is None:
        return ""
    if isinstance(handle_like, str):
        return export_anims._normalize_repo_path(handle_like)

    depot_path = export_anims._normalize_repo_path(getattr(handle_like, "DepotPath", ""))
    if depot_path:
        return depot_path

    index_obj = getattr(handle_like, "Index", None)
    for attr_name in ("Path", "DepotPath", "String"):
        depot_path = export_anims._normalize_repo_path(getattr(index_obj, attr_name, "") if index_obj is not None else "")
        if depot_path:
            return depot_path

    for handle_attr in ("Handles", "elements"):
        handles = list(getattr(handle_like, handle_attr, None) or [])
        for handle in handles:
            depot_path = export_anims._normalize_repo_path(getattr(handle, "DepotPath", ""))
            if depot_path:
                return depot_path

    return ""


def _resolve_source_cutscene_skeleton_path(entry, source_cache) -> str:
    source_path = export_anims._strip_text(entry.get("source_path", ""))
    source_index = int(entry.get("source_index", -1) or -1)
    if not source_path or source_index < 0:
        return ""
    cutscene_template = _load_cutscene_source_template(source_path, source_cache)
    animations = getattr(cutscene_template, "animations", None) or []
    if not (0 <= source_index < len(animations)):
        return ""
    animation = getattr(animations[source_index], "animation", None)
    return _extract_handle_depot_path(getattr(animation, "skeleton", None))


def _build_cutscene_export_state(context):
    scene = getattr(context, "scene", None) or getattr(bpy.context, "scene", None)
    actors = _collect_cutscene_scene_actors(scene)
    if not actors:
        return None

    export_entries = _collect_cutscene_nla_entries(context)
    source_mode = "nla"
    if not export_entries:
        export_entries = _collect_cutscene_active_entries(context)
        source_mode = "active_action"

    source_cache: Dict[str, object] = {}
    for entry in export_entries:
        entry["component"] = export_anims._normalize_cutscene_component(entry.get("component", ""))
        entry["fps"] = _resolve_cutscene_entry_fps(entry, scene, source_cache)

    return {
        "scene": scene,
        "actors": actors,
        "entries": export_entries,
        "source_mode": source_mode,
        "source_cache": source_cache,
    }


def _plan_cutscene_re_files(save_path: str, export_entries):
    resolved_save_path = _resolve_filesystem_export_path(save_path)
    base_dir = os.path.dirname(resolved_save_path)
    base_name = os.path.splitext(os.path.basename(resolved_save_path))[0]
    redkit_root = os.path.join(base_dir, f"{base_name}{CUTSCENE_RE_EXPORT_SUFFIX}")

    planned_entries = []
    for sequence_index, entry in enumerate(export_entries):
        actor_name = export_anims._strip_text(entry.get("actor_name", "")) or "actor"
        actor_folder = _sanitize_cutscene_path_part(actor_name, fallback="actor")
        component_name = export_anims._normalize_cutscene_component(entry.get("component", ""))
        action_name = export_anims._strip_text(entry.get("action_name", "")) or component_name or actor_name
        source_index = int(entry.get("source_index", -1) or -1)
        file_prefix = f"{source_index:04d}_{sequence_index:04d}" if source_index >= 0 else f"{sequence_index:04d}"
        file_parts = [file_prefix]
        if component_name and component_name != export_anims.CUTSCENE_ROOT_COMPONENT:
            file_parts.append(_sanitize_cutscene_path_part(component_name, fallback="component"))
        file_parts.append(_sanitize_cutscene_path_part(action_name, fallback="anim"))
        re_file_path = os.path.join(redkit_root, actor_folder, "_".join(file_parts) + ".re")

        planned_entry = dict(entry)
        planned_entry["component"] = component_name
        planned_entry["re_path"] = re_file_path
        planned_entry["redkit_actor_folder"] = actor_folder
        planned_entries.append(planned_entry)
    return redkit_root, planned_entries


def _write_cutscene_redkit_csv(csv_path: str, export_entries) -> None:
    parent = os.path.dirname(csv_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    lines = ["animation;component"]
    for entry in export_entries:
        animation_path = os.path.normpath(entry["re_path"])
        component_name = export_anims._normalize_cutscene_component(entry.get("component", ""))
        redkit_component = "" if component_name == export_anims.CUTSCENE_ROOT_COMPONENT else component_name
        lines.append(f"{animation_path};{redkit_component}")

    with open(csv_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")


def _export_cutscene_re_file(context, entry) -> bool:
    scene = getattr(context, "scene", None) or getattr(bpy.context, "scene", None)
    armature_obj = entry.get("armature_obj")
    action = entry.get("action")
    save_path = entry.get("re_path", "")
    fps = float(entry.get("fps", export_anims.CUTSCENE_DEFAULT_FPS) or export_anims.CUTSCENE_DEFAULT_FPS)
    frame_start = int(entry.get("frame_start", 0) or 0)
    frame_end = int(entry.get("frame_end", frame_start) or frame_start)
    frame_count = max(1, frame_end - frame_start + 1)
    anim_length = float(frame_count) / fps if fps > 0.0 else float(frame_count) / export_anims.CUTSCENE_DEFAULT_FPS

    if armature_obj is None or action is None or not save_path:
        return False

    from ..ui.ui_re_anims import _ensure_object_mode, _find_3d_override, _has_view_3d_context, _patch_re_plugin_selected_ids

    _patch_re_plugin_selected_ids()
    parent_dir = os.path.dirname(save_path)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)

    view_layer = bpy.context.view_layer
    view_objects = [obj for obj in getattr(view_layer, "objects", []) if getattr(obj, "select_get", None)]
    prev_selected = [obj for obj in view_objects if obj.select_get()]
    prev_active = view_layer.objects.active
    prev_mode = getattr(prev_active, "mode", None) if prev_active else None

    anim_data = getattr(armature_obj, "animation_data", None)
    if anim_data is None:
        anim_data = armature_obj.animation_data_create()
    prev_action = getattr(anim_data, "action", None)
    prev_action_slot = getattr(anim_data, "action_slot", None) if hasattr(anim_data, "action_slot") else None
    prev_use_nla = getattr(anim_data, "use_nla", None) if hasattr(anim_data, "use_nla") else None

    prev_frame_start = int(getattr(scene, "frame_start", 0))
    prev_frame_end = int(getattr(scene, "frame_end", 0))
    prev_frame_current = int(getattr(scene, "frame_current", 0))

    try:
        _ensure_object_mode(context)
        for obj in prev_selected:
            try:
                obj.select_set(False)
            except Exception:
                pass

        try:
            armature_obj.select_set(True)
        except Exception:
            pass
        view_layer.objects.active = armature_obj

        if prev_use_nla is not None:
            anim_data.use_nla = False
        anim_data.action = action
        if hasattr(anim_data, "action_slot"):
            action_slot = resolve_action_slot(action, target=armature_obj, ensure=True)
            if action_slot is not None:
                anim_data.action_slot = action_slot

        scene.frame_start = frame_start
        scene.frame_end = frame_end
        scene.frame_set(frame_start)

        override = {}
        if not _has_view_3d_context(context):
            override = _find_3d_override() or {}

        if override:
            with bpy.context.temp_override(**override):
                result = bpy.ops.export_animset.re(
                    'EXEC_DEFAULT',
                    filepath=save_path,
                    rotate_imported_object=False,
                    anim_length=anim_length,
                    create_root_bone=False,
                )
        else:
            result = bpy.ops.export_animset.re(
                'EXEC_DEFAULT',
                filepath=save_path,
                rotate_imported_object=False,
                anim_length=anim_length,
                create_root_bone=False,
            )
        return 'FINISHED' in result
    finally:
        anim_data.action = prev_action
        if prev_use_nla is not None:
            anim_data.use_nla = prev_use_nla
        if hasattr(anim_data, "action_slot"):
            try:
                anim_data.action_slot = prev_action_slot
            except Exception:
                pass

        scene.frame_start = prev_frame_start
        scene.frame_end = prev_frame_end
        scene.frame_set(prev_frame_current)

        for obj in view_objects:
            try:
                obj.select_set(False)
            except Exception:
                pass
        for obj in prev_selected:
            try:
                obj.select_set(True)
            except Exception:
                pass
        try:
            view_layer.objects.active = prev_active
        except Exception:
            pass
        if prev_mode and prev_active:
            try:
                bpy.ops.object.mode_set(mode=prev_mode)
            except Exception:
                pass


def export_w3_cutscene(context, savePath, export_redkit_re_files=False, export_redkit_csv=False):
    export_redkit_csv = bool(export_redkit_csv)
    export_redkit_re_files = bool(export_redkit_re_files or export_redkit_csv)

    export_state = _build_cutscene_export_state(context)
    if not export_state:
        log.error("No armatures with cutscene_actor_name found")
        return {'CANCELLED'}

    scene = export_state["scene"]
    actors = export_state["actors"]
    export_entries = list(export_state["entries"])
    source_mode = export_state["source_mode"]
    source_cache = export_state["source_cache"]

    if export_redkit_re_files:
        re_status = get_re_addon_status()
        if not re_status["enabled"]:
            log.error("RE file export requested, but blender_re_animations_plugin is not enabled")
            return {'CANCELLED'}

    resolved_save_path = _resolve_filesystem_export_path(savePath)
    csv_path = os.path.splitext(resolved_save_path)[0] + ".csv"
    if export_redkit_re_files:
        _redkit_root, export_entries = _plan_cutscene_re_files(resolved_save_path, export_entries)

    template_metadata = _collect_cutscene_template_metadata(scene, export_entries, source_cache)
    animation_groups = _group_cutscene_entries(export_entries)

    animations = []
    successful_entries = []
    for group in animation_groups:
        related_armatures = list(_iter_cutscene_related_armatures(group["armature_obj"], scene))
        skeleton_path = (
            _resolve_source_cutscene_skeleton_path(group, source_cache)
            or _resolve_cutscene_skeleton_path(group["armature_obj"], group["component"], scene=scene)
        )
        if not skeleton_path:
            log.warning(
                'No skeleton path found for "%s" on "%s"; exporting animation without a skeleton import',
                group["action_name"],
                group["armature_name"],
            )

        part_payloads = []
        for part_entry in group["parts"]:
            animation_payload = export_anims._build_cutscene_animation_from_action(
                part_entry["armature_obj"],
                part_entry["action"],
                group["actor_name"],
                group["component"],
                group["action_name"],
                part_entry["frame_start"],
                part_entry["frame_end"],
                float(part_entry.get("fps", group["fps"]) or group["fps"]),
                skeleton_path=skeleton_path,
                source_entry=part_entry,
                source_cache=source_cache,
                related_armatures=related_armatures,
            )
            if animation_payload is None:
                log.warning(
                    'No bone animation found for "%s" on "%s", skipping cutscene group',
                    group["action_name"],
                    part_entry["armature_name"],
                )
                part_payloads = []
                break
            part_payloads.append(animation_payload)

        if not part_payloads:
            continue

        if len(part_payloads) == 1:
            animations.append(part_payloads[0])
        else:
            first_frames = [int(part.get("part_start_frame", 0) or 0) for part in group["parts"]]
            total_num_frames = max(
                int(first_frames[idx]) + int(part_payload.get("num_frames", 0) or 0)
                for idx, part_payload in enumerate(part_payloads)
            )
            animations.append({
                "actor": group["actor_name"],
                "component": group["component"],
                "action_name": group["action_name"],
                "parts": part_payloads,
                "first_frames": first_frames,
                "num_frames": max(1, total_num_frames),
                "dt": float(part_payloads[0].get("dt", anims_builder.DEFAULT_DT) or anims_builder.DEFAULT_DT),
                "fps": float(part_payloads[0].get("fps", group["fps"]) or group["fps"]),
                "skeletal_type": "SAT_Normal",
                "additive_type": None,
                "motion_extraction": None,
                "skeleton_path": export_anims._normalize_repo_path(skeleton_path),
            })
        successful_entries.extend(group["entries"])

    if not animations:
        log.error("No cutscene animation data found to export")
        return {'CANCELLED'}

    cr2w = anims_builder.build_w2cutscene(
        actors=actors,
        animations=animations,
        template_metadata=template_metadata,
    )
    cr2w_writer.write_w2cutscene(cr2w, savePath)

    re_exports_done = 0
    if export_redkit_re_files:
        for entry in successful_entries:
            if _export_cutscene_re_file(context, entry):
                re_exports_done += 1
                continue
            log.error(
                'Failed to export RE file for "%s" on "%s"',
                entry.get("action_name", ""),
                entry.get("armature_name", ""),
            )
            return {'CANCELLED'}

    if export_redkit_csv:
        _write_cutscene_redkit_csv(csv_path, successful_entries)

    log.info(
        "Finished exporting cutscene with %d actors, %d animations, and %d RE files using %s source data",
        len(actors),
        len(animations),
        re_exports_done,
        source_mode,
    )
    return {'FINISHED'}
