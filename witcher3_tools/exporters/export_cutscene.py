import collections
import json
import logging
import math
import os
import re
import shutil
from pathlib import Path
import bpy
from typing import Dict, List

from .. import dialog_language
from ..CR2W import anims_builder, cr2w_writer
from ..animation.action_compat import resolve_action_slot
from ..external_addon_tools import get_re_addon_status
from ..lipsync import redkit_project
from . import export_anims


log = logging.getLogger(__name__)


CUTSCENE_TRACK_NAME = "cutscene_anim"
CUTSCENE_FACE_TRACK_NAME = f"{CUTSCENE_TRACK_NAME}_face"
CUTSCENE_SOURCE_PATH_PROP = "witcher_cutscene_source_path"
CUTSCENE_SOURCE_INDEX_PROP = "witcher_cutscene_source_index"
CUTSCENE_ANIMATION_NAME_PROP = "witcher_cutscene_animation_name"
CUTSCENE_BAKED_SOURCE_IDS_PROP = "cutscene_bake_source_clip_ids"
CUTSCENE_BAKED_SOURCE_STARTS_PROP = "cutscene_bake_source_clip_starts"
CUTSCENE_RE_EXPORT_SUFFIX = "_redkit"
CUTSCENE_DIALOG_EVENT_TYPE = "CExtAnimCutsceneDialogEvent"
CUTSCENE_ROOT_ANIMATION_NAME = "__cutsceneAnimation"
CUTSCENE_DIALOG_ID_SPACE_DEFAULT = 9999
_RADISH_STRING_ID_BASE = 2_110_000_000
_RADISH_STRING_ID_SPACE_SIZE = 1_000
_RADISH_STRING_ID_SPACE_MAX = 9_999
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


def _split_cutscene_tag_text(value) -> List[str]:
    if isinstance(value, (list, tuple, set)):
        raw_items = value
    else:
        raw_items = str(value or "").replace("\n", ";").split(";")
    return [
        export_anims._strip_text(item)
        for item in raw_items
        if export_anims._strip_text(item)
    ]


def _cutscene_event_item_payload(item) -> Dict[str, object]:
    return {
        "event_type": export_anims._strip_text(getattr(item, "event_type", "")),
        "event_name": export_anims._strip_text(getattr(item, "event_name", "")),
        "start_time": float(getattr(item, "start_time", 0.0) or 0.0),
        "duration": float(getattr(item, "duration", 0.0) or 0.0),
        "animation_name": export_anims._strip_text(getattr(item, "animation_name", "")),
        "track_name": export_anims._strip_text(getattr(item, "track_name", "")),
        "effect_name": export_anims._strip_text(getattr(item, "effect_name", "")),
        "appearance": export_anims._strip_text(getattr(item, "appearance", "")),
        "event_scope": export_anims._strip_text(getattr(item, "event_scope", "")),
        "source_index": export_anims._safe_int(getattr(item, "source_index", -1), -1),
        "always_fires_end": bool(getattr(item, "always_fires_end", False)),
    }


def _collect_scene_cutscene_events(scene):
    root_events = []
    entry_events = []
    for item in getattr(scene, "witcher_cutscene_event_items", []) or []:
        payload = _cutscene_event_item_payload(item)
        if not payload["event_type"]:
            continue
        scope = payload["event_scope"].upper()
        if scope == "ROOT":
            root_events.append(payload)
        else:
            entry_events.append(payload)
    _dialogue_lines, dialogue_events = _collect_authored_cutscene_dialogue(scene)
    if dialogue_events:
        root_events = [event for event in root_events if event["event_type"] != CUTSCENE_DIALOG_EVENT_TYPE]
        entry_events = [event for event in entry_events if event["event_type"] != CUTSCENE_DIALOG_EVENT_TYPE]
        root_events.extend(dialogue_events)
    return root_events, entry_events


def _cutscene_entry_event_matches_group(event, group) -> bool:
    source_index = export_anims._safe_int(event.get("source_index", -1), -1)
    group_parts = list(group.get("entries", []) or group.get("parts", []) or [])
    if source_index >= 0:
        for part in group_parts:
            if export_anims._safe_int(part.get("source_index", -1), -1) == source_index:
                return True
            if source_index in set(part.get("source_clip_ids", []) or []):
                return True
        return False

    event_anim_name = export_anims._strip_text(event.get("animation_name", ""))
    if not event_anim_name:
        return False

    candidates = {
        export_anims._strip_text(group.get("source_animation_name", "")),
        export_anims._strip_text(group.get("action_name", "")),
        export_anims._compose_cutscene_animation_name(
            group.get("actor_name", ""),
            group.get("component", ""),
            group.get("action_name", ""),
        ),
    }
    for part in group_parts:
        candidates.add(export_anims._strip_text(part.get("source_animation_name", "")))
        candidates.add(export_anims._strip_text(part.get("action_name", "")))
    return event_anim_name in {candidate for candidate in candidates if candidate}


def _baked_source_clip_ids(group):
    source_ids = set()
    for part in list(group.get("entries", []) or group.get("parts", []) or []):
        for value in part.get("source_clip_ids", []) or []:
            source_index = export_anims._safe_int(value, -1)
            if source_index >= 0:
                source_ids.add(source_index)
    return source_ids


def _baked_source_clip_starts(group):
    source_starts = {}
    for part in list(group.get("entries", []) or group.get("parts", []) or []):
        for source_index, start_frame in (part.get("source_clip_starts", {}) or {}).items():
            source_index = export_anims._safe_int(source_index, -1)
            if source_index >= 0:
                source_starts[source_index] = float(start_frame)
    return source_starts


def _entry_events_for_group(entry_events, group):
    events = []
    seen = set()
    default_animation_name = export_anims._compose_cutscene_animation_name(
        group.get("actor_name", ""),
        group.get("component", ""),
        group.get("action_name", ""),
    )
    baked_source_ids = _baked_source_clip_ids(group)
    baked_source_starts = _baked_source_clip_starts(group)
    for event in entry_events or []:
        if not _cutscene_entry_event_matches_group(event, group):
            continue
        payload = dict(event)
        source_index = export_anims._safe_int(payload.get("source_index", -1), -1)
        if source_index in baked_source_ids:
            payload["animation_name"] = default_animation_name
            if source_index not in baked_source_starts:
                raise ValueError(
                    f"Baked action is missing timeline provenance for source clip {source_index}; re-bake before export"
                )
            fps = float(group.get("fps", export_anims.CUTSCENE_DEFAULT_FPS) or export_anims.CUTSCENE_DEFAULT_FPS)
            baked_start = float(group.get("strip_frame_start", 0.0) or 0.0)
            payload["start_time"] = float(payload.get("start_time", 0.0) or 0.0) + (
                baked_source_starts[source_index] - baked_start
            ) / fps
        elif not payload.get("animation_name"):
            payload["animation_name"] = default_animation_name
        key = (
            payload.get("event_type"),
            payload.get("event_name"),
            payload.get("start_time"),
            payload.get("duration"),
            payload.get("animation_name"),
            payload.get("source_index"),
        )
        if key in seen:
            continue
        seen.add(key)
        events.append(payload)
    return events


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


def _companion_scene_depot_path(scene) -> str:
    repo = export_anims._strip_text(
        getattr(scene, "witcher_cutscene_export_repo_path", "")
    ).replace("/", "\\").lower()
    if not repo.endswith(".w2cutscene"):
        return ""
    folder, _sep, name = repo.rpartition("\\")
    if folder.endswith("cutscenes"):
        folder = folder[: -len("cutscenes")] + "scenes"
    return (folder + "\\" if folder else "") + name[: -len(".w2cutscene")] + ".w2scene"


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


def _resolve_strip_frame_count(strip, fallback_count: int = 1) -> int:
    fallback_count = max(1, int(fallback_count or 1))
    if strip is None:
        return fallback_count

    try:
        strip_start = float(getattr(strip, "frame_start", 0.0) or 0.0)
        strip_end = float(getattr(strip, "frame_end", strip_start) or strip_start)
    except Exception:
        return fallback_count

    if strip_end < strip_start:
        return fallback_count

    strip_count = int(round(strip_end - strip_start)) + 1
    return max(fallback_count, strip_count)


def _scene_fps(scene=None) -> float:
    scene = scene or getattr(bpy.context, "scene", None)
    render = getattr(scene, "render", None)
    fps = float(getattr(render, "fps", export_anims.CUTSCENE_DEFAULT_FPS) or export_anims.CUTSCENE_DEFAULT_FPS)
    fps_base = float(getattr(render, "fps_base", 1.0) or 1.0)
    if fps_base <= 0.0:
        fps_base = 1.0
    fps = fps / fps_base
    return fps if fps > 0.0 else export_anims.CUTSCENE_DEFAULT_FPS


def _cutscene_dialog_id_space_bounds(id_space):
    try:
        id_space = int(id_space)
    except (TypeError, ValueError) as exc:
        raise ValueError("Dialogue ID space must be a number from 0 to 9999.") from exc
    if not 0 <= id_space <= _RADISH_STRING_ID_SPACE_MAX:
        raise ValueError("Dialogue ID space must be from 0 to 9999 (-1 disables fallback allocation).")
    first_id = _RADISH_STRING_ID_BASE + id_space * _RADISH_STRING_ID_SPACE_SIZE
    return id_space, first_id, first_id + _RADISH_STRING_ID_SPACE_SIZE - 1


def _companion_scene_resource(scene):
    path = _companion_scene_depot_path(scene)
    return f'CStoryScene "{path}"' if path else ""


def authored_dialog_line_id_status(context, line_index):
    """Return the allocated ID's status and explanation."""
    scene = context.scene
    lines = scene.witcher_cutscene_dialog_lines
    raw_id = str(getattr(lines[line_index], "allocated_line_id", "") or "").strip()
    id_space_prop = getattr(scene, "witcher_cutscene_dialog_id_space", -1)
    project_path = redkit_project.get_active_project_path(context)
    info = redkit_project.next_project_line_id(project_path) if project_path else None
    if not raw_id:
        if info is not None:
            return 'INFO', f"allocated on export (next {info.next_line_id})"
        try:
            return 'INFO', f"allocated on export (from {_cutscene_dialog_id_space_bounds(id_space_prop)[1]})"
        except ValueError:
            return 'INFO', "no REDkit project or fallback ID space"
    if not raw_id.isdecimal():
        return 'ERROR', "must be numeric"
    value = int(raw_id)
    for other_index, other in enumerate(lines):
        if (
            other_index != line_index
            and str(getattr(other, "tier", "SUBTITLE") or "SUBTITLE") != "GAME"
            and str(getattr(other, "allocated_line_id", "") or "").strip() == raw_id
        ):
            return 'ERROR', f"also used by line {other_index + 1}"
    if project_path:
        name = project_path.name
        if info is None:
            return 'ERROR', f"{name} has no idSpace in its .w3edit"
        if not info.id_space <= value <= redkit_project.MAX_RADISH_LINE_ID:
            return 'ERROR', f"outside {name} idSpace (from {info.id_space})"
        owner = redkit_project.read_project_string_owners(project_path).get(value)
        if owner is None:
            return 'OK', f"free in {name}"
        if owner and owner.casefold() != _companion_scene_resource(scene).casefold():
            if str(getattr(lines[line_index], "lipsync_ref", "") or "").strip() == raw_id:
                return 'OK', f"references {owner}"
            return 'ERROR', f"taken by {owner}"
        return 'OK', f"already in {name}"
    try:
        id_space, first_id, last_id = _cutscene_dialog_id_space_bounds(id_space_prop)
    except ValueError:
        return 'INFO', "no REDkit project or fallback ID space to check"
    if first_id <= value <= last_id:
        return 'OK', f"in fallback space {id_space}"
    return 'ERROR', f"outside fallback space {id_space} ({first_id}-{last_id})"


def _write_cutscene_dialog_strings_csv(csv_path, language, rows):
    csv_path = Path(csv_path)
    output = [
        f";meta[language={str(language or 'en').strip().lower() or 'en'}]",
        "; id      |key(hex)|key(str)| text",
        ";",
    ]
    for line_id, text in rows:
        text = str(text or "")
        if not text.strip():
            raise ValueError(f"Dialogue string {line_id} has no text.")
        if any(char in text for char in ("|", "\r", "\n")):
            raise ValueError(f"Dialogue string {line_id} contains '|' or a line break, which Radish CSV cannot encode.")
        output.append(f"{int(line_id)}|||{text}")
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = csv_path.with_name(csv_path.name + ".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(output) + "\n")
        os.replace(tmp_path, csv_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
    return csv_path


def prepare_authored_cutscene_dialogue_strings(context, wrapper_path, scene_repo=""):
    scene = context.scene
    line_indices = [
        index
        for index, line in enumerate(getattr(scene, "witcher_cutscene_dialog_lines", []) or [])
        if str(getattr(line, "tier", "SUBTITLE") or "SUBTITLE") != "GAME"
    ]
    if not line_indices:
        return {"mode": "none", "path": "", "line_count": 0, "allocated_count": 0}

    language = dialog_language.get_active_text_language(context)
    project_path = redkit_project.get_active_project_path(context)
    assigned_ids = {}
    for line_index in line_indices:
        line = scene.witcher_cutscene_dialog_lines[line_index]
        if not str(getattr(line, "speaker", "") or "").strip():
            raise ValueError(f"Dialogue line {line_index + 1} has no speaker.")
        if not str(getattr(line, "text", "") or "").strip():
            raise ValueError(f"Dialogue line {line_index + 1} has no text.")
        raw_id = str(getattr(line, "allocated_line_id", "") or "").strip()
        if raw_id and not raw_id.isdigit():
            raise ValueError(f"Dialogue line {line_index + 1} has a non-numeric allocated ID: {raw_id}")
        if raw_id and int(raw_id) in assigned_ids:
            raise ValueError(
                f"Dialogue lines {assigned_ids[int(raw_id)] + 1} and {line_index + 1} share allocated ID {raw_id}."
            )
        if raw_id:
            assigned_ids[int(raw_id)] = line_index
    allocated_count = 0
    if project_path:
        project_path = Path(project_path)
        resource_path = str(scene_repo or "").replace("/", "\\")
        resource = f'CStoryScene "{resource_path}"'
        csv_path = project_path / redkit_project.PROJECT_STRINGS_CSV
        validation = redkit_project.validate_project_voice_lines(project_path)
        if validation.duplicate_ids or validation.duplicate_voiceovers or validation.invalid_ids:
            raise ValueError(f"REDkit project string CSV is invalid: {validation.compact_message()}")

        project_lines = {
            str(item.line_id or "").strip(): item
            for item in redkit_project.read_project_voice_lines(
                project_path, language=language, include_unvoiced=True,
            )
        }
        planned_ids = {}
        reserved_ids = set(assigned_ids)
        missing_indices = [
            index for index in line_indices
            if not str(getattr(scene.witcher_cutscene_dialog_lines[index], "allocated_line_id", "") or "").strip()
        ]
        next_id = None
        if missing_indices:
            id_info = redkit_project.next_project_line_id(project_path)
            if id_info is None:
                raise ValueError(f"REDkit project has no usable idSpace metadata: {project_path}")
            next_id = int(id_info.next_line_id)
        for line_index in line_indices:
            raw_id = str(
                getattr(scene.witcher_cutscene_dialog_lines[line_index], "allocated_line_id", "") or ""
            ).strip()
            if raw_id:
                planned_ids[line_index] = raw_id
                continue
            while next_id in reserved_ids or str(next_id) in project_lines:
                next_id += 1
            if next_id > redkit_project.MAX_RADISH_LINE_ID:
                raise ValueError(f"REDkit project dialogue ID space is full: {project_path}")
            planned_ids[line_index] = str(next_id)
            reserved_ids.add(next_id)
            next_id += 1

        planned_rows = []
        for line_index in line_indices:
            line = scene.witcher_cutscene_dialog_lines[line_index]
            line_id = planned_ids[line_index]
            text = str(getattr(line, "text", "") or "")
            speaker = str(getattr(line, "speaker", "") or "").strip()
            existing = project_lines.get(line_id)
            if existing is not None and existing.resource and existing.resource.lower() != resource.lower():
                if str(getattr(line, "lipsync_ref", "") or "").strip() != line_id:
                    raise ValueError(
                        f"Dialogue ID {line_id} already belongs to {existing.resource}; clear the allocated ID to allocate a new one."
                    )
                # Referenced lines stay owned by their source scene.
                text, speaker = existing.text, existing.speaker
            planned_rows.append((line_index, line_id, text, speaker, existing))

        original_ids = {
            line_index: str(
                getattr(scene.witcher_cutscene_dialog_lines[line_index], "allocated_line_id", "") or ""
            )
            for line_index in line_indices
        }
        needs_write = any(
            existing is None or existing.text != text or existing.speaker.upper() != speaker.upper()
            for _line_index, _line_id, text, speaker, existing in planned_rows
        )
        backup_path = None
        backup_dir = None
        if needs_write:
            backup_dir = redkit_project._make_backup_dir(project_path)
            backup_path = redkit_project._backup_file(project_path, backup_dir, csv_path)
        try:
            for line_index, line_id, text, speaker, existing in planned_rows:
                if not original_ids[line_index]:
                    scene.witcher_cutscene_dialog_lines[line_index].allocated_line_id = line_id
                    allocated_count += 1
                if existing is None:
                    redkit_project.add_project_line(
                        project_path,
                        line_id,
                        text,
                        speaker,
                        language=language,
                        resource=resource,
                        property_name="Line text",
                        key=line_id,
                    )
                elif existing.text != text or existing.speaker.upper() != speaker.upper():
                    redkit_project.update_project_line_csv(
                        project_path,
                        line_id,
                        line_id,
                        text=text,
                        speaker=speaker,
                        language=language,
                        backup_dir=backup_dir,
                    )
        except Exception:
            for line_index, original_id in original_ids.items():
                scene.witcher_cutscene_dialog_lines[line_index].allocated_line_id = original_id
            if backup_path is not None and Path(backup_path).is_file():
                try:
                    shutil.copy2(backup_path, csv_path)
                except Exception:
                    log.exception("Could not restore REDkit strings CSV from %s", backup_path)
            raise

        return {
            "mode": "redkit",
            "path": str(csv_path),
            "line_count": len(line_indices),
            "allocated_count": allocated_count,
        }

    id_space, first_id, last_id = _cutscene_dialog_id_space_bounds(
        getattr(scene, "witcher_cutscene_dialog_id_space", 0)
    )
    for line_index in line_indices:
        text = str(getattr(scene.witcher_cutscene_dialog_lines[line_index], "text", "") or "")
        if any(char in text for char in ("|", "\r", "\n")):
            raise ValueError(
                f"Dialogue string on line {line_index + 1} contains '|' or a line break, which Radish CSV cannot encode."
            )
    used_ids = {}
    missing_indices = []
    for line_index in line_indices:
        line = scene.witcher_cutscene_dialog_lines[line_index]
        raw_id = str(getattr(line, "allocated_line_id", "") or "").strip()
        if not raw_id:
            missing_indices.append(line_index)
            continue
        if not raw_id.isdigit() or not first_id <= int(raw_id) <= last_id:
            raise ValueError(
                f"Dialogue line {line_index + 1} allocated ID must be in {first_id}-{last_id} for id-space {id_space}."
            )
        if int(raw_id) in used_ids:
            raise ValueError(
                f"Dialogue lines {used_ids[int(raw_id)] + 1} and {line_index + 1} share allocated ID {raw_id}."
            )
        used_ids[int(raw_id)] = line_index

    candidate = first_id
    for line_index in missing_indices:
        while candidate in used_ids and candidate <= last_id:
            candidate += 1
        if candidate > last_id:
            raise ValueError(f"Dialogue id-space {id_space} is full ({first_id}-{last_id}).")
        scene.witcher_cutscene_dialog_lines[line_index].allocated_line_id = str(candidate)
        used_ids[candidate] = line_index
        allocated_count += 1
        candidate += 1

    rows = []
    for line_index in line_indices:
        line = scene.witcher_cutscene_dialog_lines[line_index]
        rows.append((int(line.allocated_line_id), str(getattr(line, "text", "") or "")))
    csv_path = Path(wrapper_path).with_suffix(".strings.csv")
    _write_cutscene_dialog_strings_csv(csv_path, language, rows)
    return {
        "mode": "csv",
        "path": str(csv_path),
        "line_count": len(line_indices),
        "allocated_count": allocated_count,
        "id_space": id_space,
    }


def _collect_authored_cutscene_dialogue(scene):
    fps = _scene_fps(scene)
    wrapper_lines = []
    root_events = []
    for line_index, line in enumerate(getattr(scene, "witcher_cutscene_dialog_lines", []) or [], 1):
        label = f"Dialogue line {line_index}"
        if not str(getattr(line, "speaker", "") or "").strip():
            raise ValueError(f"{label} has no speaker.")
        if not str(getattr(line, "text", "") or "").strip():
            raise ValueError(f"{label} has no text.")
        tier = str(getattr(line, "tier", "SUBTITLE") or "SUBTITLE")
        raw_id = getattr(line, "game_line_id", "") if tier == "GAME" else getattr(line, "allocated_line_id", "")
        try:
            string_id = int(str(raw_id or "").strip() or 0)
        except (TypeError, ValueError):
            string_id = 0
        if not 0 <= string_id <= 0xFFFFFFFF:
            string_id = 0
        if tier == "GAME" and string_id <= 0:
            raise ValueError(f"{label} has no valid numeric Game Line ID.")
        start_frame = int(getattr(line, "start_frame", 0) or 0)
        end_frame = int(getattr(line, "end_frame", 0) or 0)
        if end_frame <= start_frame:
            raise ValueError(f"{label} must end after it starts.")
        wrapper_lines.append({
            "voicetag": str(getattr(line, "speaker", "") or "").strip(),
            "string_id": string_id,
            "approved_duration": (end_frame - start_frame) / fps,
            "voice_file_name": (
                str(getattr(line, "game_voice_file_name", "") or "").strip()
                if tier == "GAME" else ""
            ),
        })
        root_events.append({
            "event_type": CUTSCENE_DIALOG_EVENT_TYPE,
            "start_time": start_frame / fps,
            "animation_name": CUTSCENE_ROOT_ANIMATION_NAME,
            "event_scope": "ROOT",
            "source_index": -1,
        })
    return wrapper_lines, root_events


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
            "tag": _split_cutscene_tag_text(actor_obj.get("cutscene_actor_tag", "")),
            "voice_tag": export_anims._strip_text(actor_obj.get("cutscene_actor_voice_tag", "")),
            "final_position": _split_cutscene_tag_text(actor_obj.get("cutscene_actor_final_position", "")),
            "kill_me": bool(actor_obj.get("cutscene_actor_kill_me", False)),
            "anim_final_pos": export_anims._strip_text(actor_obj.get("cutscene_actor_anim_final_pos", "")),
            "source_index": export_anims._safe_int(actor_obj.get(CUTSCENE_SOURCE_INDEX_PROP, -1), -1),
        })
    actors.sort(key=lambda actor: (
        1 if export_anims._safe_int(actor.get("source_index", -1), -1) < 0 else 0,
        max(0, export_anims._safe_int(actor.get("source_index", -1), -1)),
        export_anims._strip_text(actor.get("name", "")),
    ))
    return actors


def _cutscene_entry_sort_key(entry):
    source_index = export_anims._safe_int(entry.get("source_index", -1), -1)
    return (
        1 if source_index < 0 else 0,
        source_index if source_index >= 0 else 0,
        float(entry.get("strip_frame_start", 0.0) or 0.0),
        export_anims._strip_text(entry.get("actor_name", "")),
        export_anims._strip_text(entry.get("component", "")),
        export_anims._strip_text(entry.get("armature_name", "")),
        export_anims._strip_text(entry.get("strip_name", "")),
    )


def _armature_has_cutscene_camera_bone(armature_obj) -> bool:
    pose_bones = getattr(getattr(armature_obj, "pose", None), "bones", None)
    return bool(pose_bones and pose_bones.get("Camera_Node") is not None)


def _cutscene_actor_type_map(actors) -> Dict[str, str]:
    return {
        export_anims._strip_text(actor.get("name", "")): export_anims._strip_text(actor.get("type", ""))
        for actor in actors or []
        if export_anims._strip_text(actor.get("name", ""))
    }


def _is_cutscene_camera_entry(entry, actor_types_by_name=None) -> bool:
    actor_name = export_anims._strip_text(entry.get("actor_name", ""))
    actor_type = export_anims._strip_text((actor_types_by_name or {}).get(actor_name, ""))
    if actor_type == "CAT_Camera" or actor_name.lower() == "camera":
        return True
    return _armature_has_cutscene_camera_bone(entry.get("armature_obj"))


def _entry_scene_frame_range(entry):
    start = float(entry.get("strip_frame_start", entry.get("frame_start", 0.0)) or 0.0)
    end = float(entry.get("strip_frame_end", entry.get("frame_end", start)) or start)
    if end < start:
        end = start
    return start, end


def _shot_cut_ranges(scene, export_entries, actor_types_by_name):
    from ..animation import cutscene_bake

    shots = cutscene_bake.iter_shot_markers(scene) if scene is not None else []
    if not shots:
        return []
    camera_ends = [
        _entry_scene_frame_range(entry)[1]
        for entry in export_entries
        if _is_cutscene_camera_entry(entry, actor_types_by_name=actor_types_by_name)
    ]
    last_end = max([float(getattr(scene, "frame_end", 0) or 0), *camera_ends])
    # Adjacent engine parts share the boundary frame.
    return [
        (float(frame), float(shots[index + 1][2]) if index + 1 < len(shots) else last_end)
        for index, (_shot_index, _camera, frame) in enumerate(shots)
    ]


def _camera_prebake_cut_ranges(scene, actor_types_by_name):
    from ..animation import cutscene_bake

    ranges = []
    for armature_obj in _iter_scene_armatures(scene):
        entry = {"actor_name": armature_obj.get("cutscene_actor_name", ""), "armature_obj": armature_obj}
        if not _is_cutscene_camera_entry(entry, actor_types_by_name=actor_types_by_name):
            continue
        state = json.loads(armature_obj.get(cutscene_bake.PREBAKE_STATE_PROP) or "{}")
        unmuted = set(state.get("unmuted_tracks") or [])
        for track in getattr(getattr(armature_obj, "animation_data", None), "nla_tracks", []) or []:
            if track.name in unmuted and _is_cutscene_track_name(track.name):
                ranges.extend((float(strip.frame_start), float(strip.frame_end)) for strip in track.strips if not strip.mute)
    return ranges


def _cut_ranges_to_segments(ranges):
    raw_segments = sorted({(int(round(start)), int(round(end))) for start, end in ranges if end > start})
    if not raw_segments:
        return []
    base_start = raw_segments[0][0]
    return [
        {
            "scene_start": float(start),
            "scene_end": float(end),
            "part_start_frame": start - base_start,
            "part_num_frames": end - start + 1,
        }
        for start, end in raw_segments
    ]


def _collect_camera_cut_segments(export_entries, actors, scene=None):
    actor_types_by_name = _cutscene_actor_type_map(actors)
    scene = scene or getattr(bpy.context, "scene", None)
    camera_ranges = [
        _entry_scene_frame_range(entry)
        for entry in export_entries
        if _is_cutscene_camera_entry(entry, actor_types_by_name=actor_types_by_name)
    ]
    for source, ranges in (
        ("shots", _shot_cut_ranges(scene, export_entries, actor_types_by_name)),
        ("pre-bake camera strips", _camera_prebake_cut_ranges(scene, actor_types_by_name)),
        ("camera strips", camera_ranges),
    ):
        segments = _cut_ranges_to_segments(ranges)
        if segments:
            return source, segments
    return "", []


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
                    fallback_count = max(1, frame_end - frame_start + 1)
                    strip_frame_count = _resolve_strip_frame_count(strip, fallback_count=fallback_count)
                    source_clip_ids = []
                    for value in action.get(CUTSCENE_BAKED_SOURCE_IDS_PROP, []) or []:
                        source_id = export_anims._safe_int(value, -1)
                        if source_id >= 0 and source_id not in source_clip_ids:
                            source_clip_ids.append(source_id)
                    source_clip_starts = {}
                    stored_source_starts = action.get(CUTSCENE_BAKED_SOURCE_STARTS_PROP, []) or []
                    if len(stored_source_starts) == len(source_clip_ids):
                        source_clip_starts = {
                            source_id: float(stored_source_starts[index])
                            for index, source_id in enumerate(source_clip_ids)
                        }
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
                        "strip_frame_end": float(getattr(strip, "frame_end", 0.0) or 0.0),
                        "strip_scale": float(getattr(strip, "scale", 1.0) or 1.0),
                        "strip_frame_count": strip_frame_count,
                        "source_path": export_anims._strip_text(action.get(CUTSCENE_SOURCE_PATH_PROP, "")),
                        "source_index": export_anims._safe_int(action.get(CUTSCENE_SOURCE_INDEX_PROP, -1), -1),
                        "source_clip_ids": source_clip_ids,
                        "source_clip_starts": source_clip_starts,
                        "frame_start": frame_start,
                        "frame_end": frame_end,
                    })
    return sorted(entries, key=_cutscene_entry_sort_key)


def _has_cutscene_nla_strips(context):
    scene = getattr(context, "scene", None) or getattr(bpy.context, "scene", None)
    for actor_root in _collect_cutscene_actor_roots(scene):
        for armature_obj in _iter_cutscene_related_armatures(actor_root, scene):
            anim_data = getattr(armature_obj, "animation_data", None)
            for track in getattr(anim_data, "nla_tracks", []) or []:
                if _is_cutscene_track_name(getattr(track, "name", "")) and len(track.strips) > 0:
                    return True
    return False


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


def _resolve_camera_conformed_group_action_name(entries, actor_name: str, component: str) -> str:
    names = []
    seen = set()
    for entry in entries:
        source_animation_name = export_anims._strip_text(entry.get("source_animation_name", ""))
        if source_animation_name:
            _actor_name, _component_name, display_name = _split_cutscene_animation_name(source_animation_name)
            candidate = display_name or source_animation_name
        else:
            candidate = entry.get("action_name", "") or entry.get("strip_name", "")
        candidate = _strip_blender_duplicate_suffix(candidate)
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        names.append(candidate)

    if len(names) == 1:
        return names[0]
    if export_anims._strip_text(actor_name).lower() == "camera":
        return "camera"
    component = export_anims._normalize_cutscene_component(component)
    if component and component != export_anims.CUTSCENE_ROOT_COMPONENT:
        return component
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
            action_num_frames = max(
                1,
                int(entry.get("frame_end", 0) or 0) - int(entry.get("frame_start", 0) or 0) + 1,
            )
            part_num_frames = max(
                action_num_frames,
                int(entry.get("strip_frame_count", 0) or 0),
                1,
            )
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


def _camera_conformed_group_key(entry):
    return (
        export_anims._strip_text(entry.get("actor_name", "")),
        export_anims._normalize_cutscene_component(entry.get("component", "")),
    )


def _find_entry_for_camera_segment(entries, segment):
    if not entries:
        return None

    scene_start = float(segment.get("scene_start", 0.0) or 0.0)

    covering = []
    for entry in entries:
        entry_start, entry_end = _entry_scene_frame_range(entry)
        if entry_start <= scene_start <= entry_end:
            covering.append((entry_start, entry_end, entry))
    if covering:
        covering.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return covering[0][2]

    previous = []
    upcoming = []
    for entry in entries:
        entry_start, entry_end = _entry_scene_frame_range(entry)
        if entry_end <= scene_start:
            previous.append((entry_end, entry_start, entry))
        elif entry_start > scene_start:
            upcoming.append((entry_start, entry_end, entry))
    if previous:
        previous.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return previous[0][2]
    if upcoming:
        upcoming.sort(key=lambda item: (item[0], item[1]))
        return upcoming[0][2]
    return entries[0]


def _scene_frame_to_action_frame(entry, scene_frame) -> int:
    action_start = int(entry.get("frame_start", 0) or 0)
    action_end = int(entry.get("frame_end", action_start) or action_start)
    strip_start, strip_end = _entry_scene_frame_range(entry)
    if action_end < action_start:
        action_end = action_start

    scene_frame = float(scene_frame)
    if scene_frame <= strip_start:
        return action_start
    if scene_frame >= strip_end:
        return action_end

    scale = float(entry.get("strip_scale", 1.0) or 1.0)
    if abs(scale) <= 1e-6:
        scale = 1.0
    action_frame = action_start + ((scene_frame - strip_start) / scale)
    return max(action_start, min(action_end, int(round(action_frame))))


def _group_cutscene_entries_to_camera_segments(export_entries, camera_segments):
    grouped_entries = collections.OrderedDict()
    for entry in sorted(export_entries, key=_cutscene_entry_sort_key):
        group_key = _camera_conformed_group_key(entry)
        grouped_entries.setdefault(group_key, []).append(entry)

    groups = []
    for entries in grouped_entries.values():
        ordered_entries = sorted(
            entries,
            key=lambda entry: (
                float(entry.get("strip_frame_start", 0.0) or 0.0),
                float(entry.get("strip_frame_end", 0.0) or 0.0),
                export_anims._strip_text(entry.get("strip_name", "")),
            ),
        )
        if not ordered_entries:
            continue

        actor_name = export_anims._strip_text(ordered_entries[0].get("actor_name", ""))
        component = export_anims._normalize_cutscene_component(ordered_entries[0].get("component", ""))
        action_name = _resolve_camera_conformed_group_action_name(ordered_entries, actor_name, component)

        parts = []
        total_num_frames = 0
        for part_index, segment in enumerate(camera_segments):
            source_entry = _find_entry_for_camera_segment(ordered_entries, segment)
            if source_entry is None:
                continue

            part_num_frames = max(1, int(segment.get("part_num_frames", 0) or 0))
            part_start_frame = max(0, int(segment.get("part_start_frame", 0) or 0))
            sample_frame_start = _scene_frame_to_action_frame(source_entry, segment.get("scene_start", 0.0))

            part_entry = dict(source_entry)
            part_entry["part_index"] = part_index
            part_entry["part_start_frame"] = part_start_frame
            part_entry["part_num_frames"] = part_num_frames
            part_entry["action_name"] = action_name
            part_entry["source_animation_name"] = export_anims._compose_cutscene_animation_name(
                actor_name,
                component,
                action_name,
            )
            part_entry["frame_start"] = sample_frame_start
            part_entry["frame_end"] = sample_frame_start + part_num_frames - 1
            part_entry["camera_segment_start"] = float(segment.get("scene_start", 0.0) or 0.0)
            part_entry["camera_segment_end"] = float(segment.get("scene_end", 0.0) or 0.0)
            parts.append(part_entry)
            total_num_frames = max(total_num_frames, part_start_frame + part_num_frames)

        if not parts:
            continue

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
            "strip_frame_start": float(camera_segments[0].get("scene_start", 0.0) or 0.0),
            "num_frames": max(1, total_num_frames),
            "parts": parts,
            "entries": parts,
            "conformed_to_camera_cuts": True,
        })
    return groups


def _sync_multipart_group_timings(groups):
    """Use the longest scene-derived timing for groups that share the same cut layout."""
    references = {}
    for group in groups:
        parts = list(group.get("parts", []) or [])
        if len(parts) <= 1:
            continue
        first_frames = tuple(int(part.get("part_start_frame", 0) or 0) for part in parts)
        key = (len(parts), first_frames)
        part_lengths = [max(1, int(part.get("part_num_frames", 0) or 0)) for part in parts]
        total_num_frames = max(
            int(first_frames[idx]) + part_lengths[idx]
            for idx in range(len(parts))
        )
        score = (total_num_frames, sum(part_lengths))
        current = references.get(key)
        if current is None or score > current["score"]:
            references[key] = {
                "part_lengths": part_lengths,
                "num_frames": total_num_frames,
                "score": score,
            }

    for group in groups:
        parts = list(group.get("parts", []) or [])
        if len(parts) <= 1:
            continue
        first_frames = tuple(int(part.get("part_start_frame", 0) or 0) for part in parts)
        reference = references.get((len(parts), first_frames))
        if not reference:
            continue
        for part, part_length in zip(parts, reference["part_lengths"]):
            part["part_num_frames"] = max(1, int(part_length or 1))
        group["num_frames"] = max(1, int(reference["num_frames"] or 1))

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
    source_index = export_anims._safe_int(entry.get("source_index", -1), -1)
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
    if not export_entries and not _has_cutscene_nla_strips(context):
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
        source_index = export_anims._safe_int(entry.get("source_index", -1), -1)
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

    root_events, entry_events = _collect_scene_cutscene_events(scene)
    template_metadata = _collect_cutscene_template_metadata(scene, export_entries, source_cache)
    companion_scene = _companion_scene_depot_path(scene)
    used_in_files = list(template_metadata.get("usedInFiles") or [])
    if companion_scene and bool(getattr(scene, "witcher_cutscene_dialog_lines", None)):
        companion_key = companion_scene.replace("/", "\\").lower()
        template_metadata["usedInFiles"] = [
            companion_scene,
            *[
                path for path in used_in_files
                if export_anims._strip_text(path).replace("/", "\\").lower() != companion_key
            ],
        ]
        log.info("usedInFiles prioritized authored companion scene '%s'", companion_scene)
    elif companion_scene and not used_in_files:
        template_metadata["usedInFiles"] = [companion_scene]
        log.info("usedInFiles defaulted to companion scene '%s'", companion_scene)
    if root_events:
        template_metadata["animevents"] = root_events
    cut_source, camera_segments = _collect_camera_cut_segments(export_entries, actors, scene=scene)
    if camera_segments:
        animation_groups = _group_cutscene_entries_to_camera_segments(export_entries, camera_segments)
        log.info(
            "Conforming cutscene export to %d cut segment(s) from %s",
            len(camera_segments),
            cut_source,
        )
    else:
        animation_groups = _sync_multipart_group_timings(_group_cutscene_entries(export_entries))

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
            part_num_frames = max(1, int(part_entry.get("part_num_frames", 0) or 0))
            sample_frame_start = int(part_entry.get("frame_start", 0) or 0)
            sample_frame_end = sample_frame_start + part_num_frames - 1
            animation_payload = export_anims._build_cutscene_animation_from_action(
                part_entry["armature_obj"],
                part_entry["action"],
                group["actor_name"],
                group["component"],
                group["action_name"],
                sample_frame_start,
                sample_frame_end,
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
            animation_payload["num_frames"] = part_num_frames
            part_payloads.append(animation_payload)

        if not part_payloads:
            continue

        if len(part_payloads) == 1:
            part_payloads[0]["entry_events"] = _entry_events_for_group(entry_events, group)
            animations.append(part_payloads[0])
        else:
            first_frames = [int(part.get("part_start_frame", 0) or 0) for part in group["parts"]]
            total_num_frames = max(
                int(first_frames[idx]) + int(part_payload.get("num_frames", 0) or 0)
                for idx, part_payload in enumerate(part_payloads)
            )
            multipart_dt = float(part_payloads[0].get("dt", anims_builder.DEFAULT_DT) or anims_builder.DEFAULT_DT)
            multipart_fps = float(part_payloads[0].get("fps", group["fps"]) or group["fps"])

            animations.append({
                "actor": group["actor_name"],
                "component": group["component"],
                "action_name": group["action_name"],
                "parts": part_payloads,
                "first_frames": first_frames,
                "num_frames": max(1, total_num_frames),
                "dt": multipart_dt,
                "fps": multipart_fps,
                "skeletal_type": "SAT_Normal",
                "additive_type": None,
                "motion_extraction": None,
                "skeleton_path": export_anims._normalize_repo_path(skeleton_path),
                "entry_events": _entry_events_for_group(entry_events, group),
            })
        successful_entries.extend(group["entries"])

    if not animations:
        log.error("No cutscene animation data found to export")
        return {'CANCELLED'}

    from ..animation.cutscene_bake import SCAFFOLD_ACTORS

    animated_actor_names = {export_anims._strip_text(anim.get("actor", "")) for anim in animations}
    animated_actor_names |= {a["name"] for a in actors if str(a["name"]).lower() in SCAFFOLD_ACTORS}
    stale_actors = [a for a in actors if a["name"] not in animated_actor_names]
    if stale_actors:
        log.warning(
            "Dropping %d actor def(s) without animations from the export: %s",
            len(stale_actors),
            ", ".join(a["name"] for a in stale_actors),
        )
        actors = [a for a in actors if a["name"] in animated_actor_names]

    cr2w = anims_builder.build_w2cutscene(
        actors=actors,
        animations=animations,
        template_metadata=template_metadata,
    )
    cr2w_writer.write_w2cutscene(cr2w, savePath)

    re_exports_done = 0
    if export_redkit_re_files:
        _redkit_root, successful_entries = _plan_cutscene_re_files(resolved_save_path, successful_entries)
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
