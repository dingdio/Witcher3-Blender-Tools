import logging
import os

from .CR2W_file import read_CR2W
from .prop_utils import prop_to_string as _prop_to_str
from ..dialog_language import resolve_localized_text


log = logging.getLogger(__name__)


def _localized_string_id_and_text(prop, scene_filepath=""):
    string_obj = getattr(prop, "String", None) if prop is not None else None
    if string_obj is None:
        return "", ""

    line_id = str(getattr(string_obj, "val", "") or "").strip()
    text = ""
    try:
        text = str(getattr(string_obj, "text", "") or "").strip()
    except Exception:
        log.debug("Could not resolve W2 localized dialog string %s", line_id, exc_info=True)
    if not text:
        text = resolve_localized_text(line_id, scene_filepath, source_game="W2")
    return line_id, text


def _normalize_scene_path(value):
    value = str(value or "").replace("/", "\\").strip().lower()
    return value.lstrip("\\")


def _cutscene_handle_matches(depot_path, cutscene_path):
    depot_path = _normalize_scene_path(depot_path)
    cutscene_path = _normalize_scene_path(cutscene_path)
    if not depot_path or not cutscene_path:
        return False
    if depot_path == cutscene_path or cutscene_path.endswith(depot_path):
        return True
    return os.path.basename(depot_path) == os.path.basename(cutscene_path)


def _handle_depot_path(handle_prop):
    for handle in getattr(handle_prop, "Handles", None) or []:
        depot_path = str(getattr(handle, "DepotPath", "") or "").strip()
        if depot_path:
            return depot_path
    return _prop_to_str(handle_prop) or ""


def _chunk_by_ptr(chunks, ptr_value):
    try:
        idx = int(ptr_value) - 1
    except (TypeError, ValueError):
        return None
    if 0 <= idx < len(chunks):
        return chunks[idx]
    return None


def _section_scene_element_chunks(section_chunk, chunks):
    elements_prop = section_chunk.GetVariableByName("sceneElements")
    if elements_prop is None:
        return []
    out = []
    for ptr_value in getattr(elements_prop, "value", None) or []:
        chunk = _chunk_by_ptr(chunks, ptr_value)
        if chunk is not None:
            out.append(chunk)
    return out


def _line_from_chunk(line_chunk, scene_filepath):
    actor = _prop_to_str(line_chunk.GetVariableByName("voicetag"))
    voice = _prop_to_str(line_chunk.GetVariableByName("voiceFileName"))
    sound_event = _prop_to_str(line_chunk.GetVariableByName("soundEventName"))

    line_id, line_text = _localized_string_id_and_text(
        line_chunk.GetVariableByName("dialogLine"),
        scene_filepath,
    )
    try:
        line_index = int(line_id or 0)
    except (ValueError, TypeError):
        line_index = 0

    return {
        "actor": actor,
        "voice_file": voice,
        "sound_event": sound_event,
        "line_id": line_id,
        "line_index": line_index,
        "line_text": line_text,
        "source_game": "W2",
    }


def get_cutscene_dialog_lines(scene_filepath, cutscene_path):
    try:
        the_file = read_CR2W(scene_filepath)
    except Exception:
        log.exception("Failed to open W2 .w2scene for dialog lookup: %s", scene_filepath)
        return []

    chunks = list(getattr(getattr(the_file, "CHUNKS", None), "CHUNKS", None) or [])
    if not chunks:
        return []

    lines = []
    for chunk in chunks:
        if getattr(chunk, "name", "") != "CStorySceneCutsceneSection":
            continue

        cutscene_prop = chunk.GetVariableByName("cutscene")
        if cutscene_prop is None:
            continue
        if not _cutscene_handle_matches(_handle_depot_path(cutscene_prop), cutscene_path):
            continue

        for element_chunk in _section_scene_element_chunks(chunk, chunks):
            if getattr(element_chunk, "name", "") != "CStorySceneLine":
                continue
            lines.append(_line_from_chunk(element_chunk, scene_filepath))

    log.info("Loaded %d W2 dialog lines from %s", len(lines), os.path.basename(scene_filepath))
    return lines
