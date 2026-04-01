import os
import logging
from .w3_types import CStoryScene
from .CR2W_types import getCR2W, W_CLASS
from .prop_utils import prop_to_string as _prop_to_str

log = logging.getLogger(__name__)



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

    Returns a list of dicts with keys: actor, voice_file, sound_event, line_index.
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

            line_idx = 0
            dl_prop = el_chunk.GetVariableByName('dialogLine')
            if dl_prop is not None:
                ls = getattr(dl_prop, 'String', None)
                if ls is not None:
                    try:
                        line_idx = int(getattr(ls, 'val', 0) or 0)
                    except (ValueError, TypeError):
                        line_idx = 0

            lines.append({
                "actor":       actor,
                "voice_file":  voice,
                "sound_event": snd_evt,
                "line_index":  line_idx,
            })

        # Only process the first matching section per file
        break

    log.info("Loaded %d dialog lines from %s", len(lines), os.path.basename(scene_filepath))
    return lines
