import os
import logging
from .w3_types import CStoryScene
from .CR2W_types import getCR2W, W_CLASS
from .bStream import open_cr2w_read_stream
from .prop_utils import prop_to_string as _prop_to_str
from ..dialog_language import resolve_localized_text
from ..repo_paths import source_game_from_version

log = logging.getLogger(__name__)


def _localized_string_id_and_text(prop, scene_filepath=""):
    string_obj = getattr(prop, 'String', None) if prop is not None else None
    if string_obj is None:
        return "", ""

    line_id = str(getattr(string_obj, 'val', "") or "").strip()
    text = ""
    try:
        text = str(getattr(string_obj, 'text', "") or "").strip()
    except Exception:
        log.debug("Could not resolve localized dialog string %s", line_id, exc_info=True)
    if not text and line_id and scene_filepath:
        csv_path = os.path.splitext(str(scene_filepath))[0] + ".strings.csv"
        try:
            with open(csv_path, "r", encoding="utf-8-sig", errors="replace") as handle:
                for raw_line in handle:
                    key, separator, value = raw_line.rstrip("\r\n").partition("|||")
                    if separator and key.strip() == line_id:
                        text = value
                        break
        except OSError:
            pass
    if not text:
        text = resolve_localized_text(line_id, scene_filepath)
    return line_id, text


def create_scene(file):
    storyScene = CStoryScene()
    version = int(getattr(getattr(file, "HEADER", None), "version", 999) or 999)
    storyScene.version = version
    storyScene.source_game = source_game_from_version(version)
    storyScene.chunksRef = file.CHUNKS.CHUNKS
    storyScene.LocalizedStringsRef = file.LocalizedStrings
    chunk:W_CLASS
    for chunk in file.CHUNKS.CHUNKS:
        if chunk.name == "CStoryScene":
            for prop in chunk.PROPS:
                setattr(storyScene, prop.theName, prop)
            #storyScene.sceneTemplates = chunk.GetVariableByName('sceneTemplates')
        elif chunk.name == "CStorySceneLine":
            #skelly = read_skelly(chunk)
            break
    return storyScene #skelly


def load_bin_scene(fileName):
    theFile = getCR2W(open_cr2w_read_stream(fileName))
    return create_scene(theFile)


def _section_variant_durations(section, chunks):
    durations = {}
    variants = section.GetVariableByName('variants')
    for ptr_val in getattr(variants, 'value', None) or []:
        if not ptr_val or ptr_val <= 0 or ptr_val > len(chunks):
            continue
        element_info = chunks[ptr_val - 1].GetVariableByName('elementInfo')
        for info in getattr(element_info, 'More', None) or getattr(element_info, 'elements', None) or []:
            member = getattr(info, 'GetVariableByName', None)
            if member is None:
                member = lambda name, info=info: next(
                    (prop for prop in getattr(info, 'More', None) or [] if prop.theName == name), None
                )
            element_id = _prop_to_str(member('elementId'))
            duration = member('approvedDuration')
            if element_id and duration is not None:
                durations[element_id] = float(getattr(duration, 'Value', 0.0) or 0.0)
        if durations:
            break
    return durations


def get_cutscene_dialog_lines(scene_filepath, cutscene_path):
    """Load dialog lines from a .w2scene that belong to the given .w2cutscene.

    Finds CStorySceneCutsceneSection chunks whose 'cutscene' handle depot path
    matches cutscene_path (compared by basename), then extracts CStorySceneLine
    data from that section's sceneElements array.

    Returns a list of dicts with keys: actor, voice_file, sound_event, line_id,
    line_index, line_text.
    """
    try:
        theFile = getCR2W(open_cr2w_read_stream(scene_filepath))
    except Exception:
        log.exception("Failed to open .w2scene for dialog lookup: %s", scene_filepath)
        return []

    if int(getattr(getattr(theFile, "HEADER", None), "version", 999) or 999) <= 115:
        from .dc_scene_w2 import get_cutscene_dialog_lines as get_w2_cutscene_dialog_lines

        return get_w2_cutscene_dialog_lines(scene_filepath, cutscene_path)

    CHUNKS = theFile.CHUNKS.CHUNKS
    cs_basename = os.path.basename(str(cutscene_path or "")).lower()
    if not cs_basename:
        return []

    lines = []
    for chunk in CHUNKS:
        if chunk.name != "CStorySceneCutsceneSection":
            continue

        # Match via the handle's DepotPath
        cutscene_prop = chunk.GetVariableByName('cutscene')
        if cutscene_prop is None:
            continue
        handles = getattr(cutscene_prop, 'Handles', None)
        if not handles:
            continue
        depot_path = getattr(handles[0], 'DepotPath', '') or ''
        if os.path.basename(str(depot_path)).lower() != cs_basename:
            continue

        # Found matching section; walk sceneElements.
        elements_prop = chunk.GetVariableByName('sceneElements')
        if elements_prop is None:
            continue
        ptr_list = getattr(elements_prop, 'value', None) or []
        durations = _section_variant_durations(chunk, CHUNKS)

        for ptr_val in ptr_list:
            if not ptr_val or ptr_val <= 0 or ptr_val > len(CHUNKS):
                continue
            el_chunk = CHUNKS[ptr_val - 1]
            if el_chunk.name != "CStorySceneLine":
                continue

            actor    = _prop_to_str(el_chunk.GetVariableByName('voicetag'))
            voice    = _prop_to_str(el_chunk.GetVariableByName('voiceFileName'))
            snd_evt  = _prop_to_str(el_chunk.GetVariableByName('soundEventName'))

            dl_prop = el_chunk.GetVariableByName('dialogLine')
            line_id, line_text = _localized_string_id_and_text(dl_prop, scene_filepath)
            try:
                line_idx = int(line_id or 0)
            except (ValueError, TypeError):
                line_idx = 0

            lines.append({
                "actor":       actor,
                "voice_file":  voice,
                "sound_event": snd_evt,
                "line_id":     line_id,
                "line_index":  line_idx,
                "line_text":   line_text,
                "approved_duration": durations.get(
                    _prop_to_str(el_chunk.GetVariableByName('elementID')), 0.0
                ),
                "source_game": "W3",
            })

    log.info("Loaded %d dialog lines from %s", len(lines), os.path.basename(scene_filepath))
    return lines
