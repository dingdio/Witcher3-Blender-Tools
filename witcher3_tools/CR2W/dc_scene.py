import os
import logging
from .w3_types import CStoryScene
from .CR2W_types import getCR2W, W_CLASS
from .prop_utils import prop_to_string as _prop_to_str

log = logging.getLogger(__name__)

_SCENE_W3STRINGS_CACHE = {}


def _scene_source_root_candidates(scene_filepath):
    source_path = str(scene_filepath or "").strip().replace("/", "\\")
    if not source_path or not os.path.isabs(source_path):
        return []

    roots = []
    normalized = os.path.normpath(source_path)
    lowered = normalized.lower()
    for marker in ("\\r4data\\", "\\workspace\\", "\\content\\content0\\"):
        marker_idx = lowered.find(marker)
        if marker_idx >= 0:
            root = normalized[:marker_idx + len(marker) - 1]
            if os.path.isdir(root):
                roots.append(root)
    return roots


def _load_w3strings_map(strings_path):
    strings_path = os.path.normpath(str(strings_path or ""))
    if not strings_path:
        return {}
    strings_key = os.path.normcase(strings_path)
    cached = _SCENE_W3STRINGS_CACHE.get(strings_key)
    if cached is not None:
        return cached

    lines = {}
    try:
        from .witcher_cache.W3Strings.W3StringFile import W3StringFile
        from .bStream import bStream

        string_file = W3StringFile()
        with open(strings_path, "rb") as reader:
            stream = bStream(path=strings_path, reader=reader)
            string_file.Read(stream)
        for item in string_file.block1:
            lines[int(item.str_id)] = item.str
    except Exception:
        log.debug("Could not read scene string table: %s", strings_path, exc_info=True)

    _SCENE_W3STRINGS_CACHE[strings_key] = lines
    return lines


def _scene_localized_text(line_id, scene_filepath):
    try:
        line_key = int(str(line_id or "").strip())
    except (TypeError, ValueError):
        return ""
    if not line_key:
        return ""

    try:
        from .witcher_cache.W3Strings.W3StringManager import Configuration
        language = str(getattr(Configuration, "TextLanguage", "en") or "en").strip()
    except Exception:
        language = "en"
    if not language:
        language = "en"

    for root in _scene_source_root_candidates(scene_filepath):
        strings_path = os.path.join(root, f"{language}.w3strings")
        if os.path.isfile(strings_path):
            text = _load_w3strings_map(strings_path).get(line_key, "")
            if text:
                return text
    return ""


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
    if not text:
        text = _scene_localized_text(line_id, scene_filepath)
    return line_id, text


def create_scene(file):
    storyScene = CStoryScene()
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
    with open(fileName,"rb") as f:
        theFile = getCR2W(f)
    return create_scene(theFile)
def get_cutscene_dialog_lines(scene_filepath, cutscene_path):
    """Load dialog lines from a .w2scene that belong to the given .w2cutscene.

    Finds CStorySceneCutsceneSection chunks whose 'cutscene' handle depot path
    matches cutscene_path (compared by basename), then extracts CStorySceneLine
    data from that section's sceneElements array.

    Returns a list of dicts with keys: actor, voice_file, sound_event, line_id,
    line_index, line_text.
    """
    try:
        with open(scene_filepath, "rb") as f:
            theFile = getCR2W(f)
    except Exception:
        log.exception("Failed to open .w2scene for dialog lookup: %s", scene_filepath)
        return []

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
            })

    log.info("Loaded %d dialog lines from %s", len(lines), os.path.basename(scene_filepath))
    return lines
