# WAV Lipsync Module

This module runs the Radish/w3 lipsync tools as external processes. Set the
external `radish-tools` folder from **Radish Lipsync 4 REDkit** in Add-on
Preferences before generating lipsync. The normal Radish Modding Tools package
is not enough for this workflow.
The generation pipeline:

1. generate `.phonemes`
2. generate `.lipsyncanim.csv` with `w3speech-lipsync-creator`
3. import the CSV morph curves onto `w3_face_poses`
4. optionally generate Wwise `.wem` audio with `WwiseConsole`
5. optionally generate REDkit `.re` output with `w3speech-converter`

The UI supports two generation modes:

- `Import WAV` can optionally auto-transcribe the WAV first when a ggml Whisper
  model path is configured. `ggml-large-v3-turbo-q5_0.bin` is the recommended
  offline CPU model; the old bundled tiny model was too inaccurate for this
  workflow. The panel can download the recommended model from the whisper.cpp
  Hugging Face repository into the extension user data directory and verifies
  the SHA1 before using it. Recognized text is written into the `Voiceline`
  field only when no text is already set, then used for Radish extraction and
  subtitle strip tagging. It imports the post-extractor WAV; Radish may rename
  the WAV to the padded Witcher line filename, so the module resolves that
  renamed file before creating the Blender sound strip.
- `Generate From Text` runs the extractor's `--generate-from-text-only` mode,
  creates a silent WAV strip so the subtitle overlay has timed strip data, and
  imports the generated Radish morph CSV.

When the UI `Line ID` field is empty, generated IDs come from the active REDkit
project if one is configured. The module reads `<project>/*.w3edit` for
`idSpace`, scans the project's root
`LocalEditorStringDataBaseW3_UTF8_mod_export.csv`, and picks the next unused ID.
If no REDkit project ID space is available, it falls back to a safe 32-bit range
accepted by the Radish extractor. Generating again with the same line ID replaces
the previous sound strip from this module, so subtitle text and imported audio
update instead of duplicating.

The panel includes a local `Lipsync Lines` editor for creating and editing
custom lines. `New` creates a fresh line ID, `Import/Replace WAV` transcribes
the selected WAV, generates lipsync, and stores the result on the active line.
Generated lines are kept in the Blender scene so the active line can be edited
or have its WAV replaced later. Selecting a line always updates the active edit
fields. `Load on Select` controls whether selecting a generated line also loads
its stored audio and Radish morph CSV onto the target character. `Replace Audio`
mirrors the Dialogue Browser option and clears existing sound strips before
importing generated lipsync audio. New lines derive the speaker from the current
target armature when imported character metadata can be recognized, with manual
override available from the editable `Speaker` field. The editor can also add
the selected Dialog Browser entry as a starting point, or load metadata from a
selected Sequencer sound strip. Applying edits to a selected strip updates
subtitle/dialog metadata only; regenerate if the mouth animation needs to match
changed words.

Expected external Radish files:

- `radish-tools/w3speech-phoneme-extractor.exe`
- `radish-tools/w3speech-lipsync-creator.exe`
- `radish-tools/w3speech-converter.exe`
- `radish-tools/espeak_lib.dll`
- `radish-tools/data/`
- `radish-tools/repo.lipsync/`

The add-on does not bundle these binaries. Users must link an existing
Radish Lipsync 4 REDkit `radish-tools` install in Add-on Preferences or set
`W3_RADISH_LIPSYNC_TOOLS`.

Downloads and WEM requirements:

- Radish Lipsync 4 REDkit: https://www.nexusmods.com/witcher3/mods/9914
- WEM generation requires Audiokinetic Wwise 2021.1.x; Radish recommends
  v2021.1.14. Install it through the Audiokinetic Launcher:
  https://www.audiokinetic.com/en/download/
- Set Add-on Preferences > External Tools > Wwise Console to `WwiseConsole.exe`
  or its `Authoring/x64/Release/bin` folder. If left empty, the add-on checks
  the Radish template `_settings_.bat`, `WITCHER_LIPSYNC_WWISE_BIN`, `WWISE_BIN`,
  `WWISECONSOLE`, `PATH`, and common Program Files install locations.
- Bundled `vgmstream` can decode existing `.wem` files, but cannot encode WAV
  audio into REDkit-compatible `.wem`.
