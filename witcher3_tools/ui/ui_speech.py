
import logging
import os
import re
import subprocess
import time
from pathlib import Path
log = logging.getLogger(__name__)

from .. import fbx_util, file_helpers
from .. import (
    get_uncook_path,
    get_W3_VOICE_PATH,
    get_W3_OGG_PATH,
    get_vgmstream_path,
    get_all_addon_prefs,
    get_game_path,
    get_w2_unbundle_path,
    get_witcher2_game_path,
)
from ..CR2W.witcher_cache.Speech import LoadSpeechManager
from ..CR2W.witcher_cache.Speech.W3Speech import pad_filename
from ..importers import import_anims, import_rig
from ..exporters import export_anims
from ..ui.ui_utils import WITCH_PT_Base
from .. import dialog_language

import bpy
from bpy.types import Panel, Operator, UIList, PropertyGroup
from bpy.props import IntProperty, StringProperty, CollectionProperty, FloatProperty, BoolProperty
from bpy_extras.io_utils import (
        ImportHelper,
        ExportHelper
        )

def _get_active_armature(context):
    """Resolve the target armature using the character system first, then fallbacks."""
    from .armature_context import get_main_armature
    armature = get_main_armature(context, prefer_active=True, remember=False, fallback=True)
    if armature:
        return armature
    obj = context.active_object
    if obj and obj.type == 'ARMATURE':
        return obj
    if obj and obj.type == 'MESH' and obj.parent and obj.parent.type == 'ARMATURE':
        return obj.parent
    for obj in context.selected_objects:
        if obj.type == 'ARMATURE':
            return obj
    return None

def _armature_has_face_morphs(armature):
    return bool(
        armature
        and armature.pose
        and (
            "w3_face_poses" in armature.pose.bones
            or "w2_face_poses" in armature.pose.bones
        )
    )


_W2_VO_ID_RE = re.compile(r"^VO_ID\d+$", re.IGNORECASE)
_W2_LOCAL_SPEECH_LANG_RE = re.compile(r"(?:^|[\\/])local_speech[\\/]([^\\/]+)[\\/]", re.IGNORECASE)


def _w2_voice_stem_from_path(path_value):
    stem = Path(str(path_value or "")).stem
    return stem if _W2_VO_ID_RE.match(stem or "") else ""


def _w2_voice_language_from_path(path_value):
    match = _W2_LOCAL_SPEECH_LANG_RE.search(str(path_value or ""))
    return match.group(1).lower() if match else ""


def _w2_local_speech_bases(context):
    # Witcher 2 support: localized speech can be loose under the game install
    # (CookedPC/local_speech or data/local_speech) or under a configured W2 export.
    candidates = []
    game_root = str(get_witcher2_game_path(context) or "").strip()
    if game_root:
        candidates.append(Path(game_root) / "CookedPC")
        candidates.append(Path(game_root) / "data")
    unbundle_root = str(get_w2_unbundle_path(context) or "").strip()
    if unbundle_root:
        candidates.append(Path(unbundle_root))
        candidates.append(Path(unbundle_root) / "data")

    out = []
    seen = set()
    for base in candidates:
        try:
            key = os.path.normcase(os.path.normpath(str(base)))
        except Exception:
            key = str(base).lower()
        if key not in seen:
            seen.add(key)
            out.append(base)
    return out


def _w2_language_candidates(context, path_value="", language=None):
    candidates = []
    for value in (
        language,
        _w2_voice_language_from_path(path_value),
        dialog_language.get_active_voice_language(context),
        "en",
    ):
        value = dialog_language.normalize_dialog_language(value or "").lower()
        if value and value not in candidates:
            candidates.append(value)
    return candidates


def _w2_pair_path_from_sibling(path_value, target_ext):
    path = Path(path_value)
    target_ext = target_ext.lower()
    parts = list(path.parts)
    lower_parts = [part.lower() for part in parts]
    try:
        if target_ext == ".mp2":
            idx = lower_parts.index("lipsync")
            parts[idx] = "audio"
        elif target_ext == ".dat":
            idx = lower_parts.index("audio")
            parts[idx] = "lipsync"
        else:
            return ""
    except ValueError:
        return ""
    candidate = Path(*parts).with_suffix(target_ext)
    return str(candidate) if candidate.is_file() else ""


def _resolve_w2_voice_repo_path(path_value, context, language=None):
    raw_path = str(path_value or "").replace("/", "\\").strip()
    if not raw_path:
        return ""
    if os.path.isabs(raw_path):
        return raw_path if os.path.exists(raw_path) else ""

    rel_path = raw_path.lstrip("\\")
    langs = _w2_language_candidates(context, rel_path, language=language)
    for base in _w2_local_speech_bases(context):
        direct = base / rel_path
        if direct.is_file():
            return str(direct)
        if rel_path.lower().startswith("local_speech\\"):
            continue
        stem = _w2_voice_stem_from_path(rel_path)
        ext = Path(rel_path).suffix.lower()
        folder = "lipsync" if ext == ".dat" else "audio" if ext == ".mp2" else ""
        if stem and folder:
            for lang in langs:
                candidate = base / "local_speech" / lang / folder / f"{stem}{ext}"
                if candidate.is_file():
                    return str(candidate)
    return ""


def _resolve_w2_voice_pair(path_value, context, language=None):
    path = str(path_value or "").strip()
    if not path:
        return "", ""
    if not os.path.isabs(path) or not os.path.exists(path):
        resolved = _resolve_w2_voice_repo_path(path, context, language=language)
        if resolved:
            path = resolved

    ext = Path(path).suffix.lower()
    dat_path = path if ext == ".dat" else ""
    mp2_path = path if ext == ".mp2" else ""
    stem = _w2_voice_stem_from_path(path)
    langs = _w2_language_candidates(context, path, language=language)

    if dat_path and not mp2_path:
        mp2_path = _w2_pair_path_from_sibling(dat_path, ".mp2")
    elif mp2_path and not dat_path:
        dat_path = _w2_pair_path_from_sibling(mp2_path, ".dat")

    if stem:
        for base in _w2_local_speech_bases(context):
            for lang in langs:
                if not dat_path:
                    candidate = base / "local_speech" / lang / "lipsync" / f"{stem}.dat"
                    if candidate.is_file():
                        dat_path = str(candidate)
                if not mp2_path:
                    candidate = base / "local_speech" / lang / "audio" / f"{stem}.mp2"
                    if candidate.is_file():
                        mp2_path = str(candidate)
                if dat_path and mp2_path:
                    break
            if dat_path and mp2_path:
                break

    return dat_path, mp2_path


def _import_w2_sound_strip(context, sound_path, at_frame=0):
    scene = context.scene
    if not scene.sequence_editor:
        scene.sequence_editor_create()
    from .ui_voice import _get_next_sound_channel, _get_sequence_editor_strips

    strips = _get_sequence_editor_strips(scene.sequence_editor)
    if strips is None:
        raise RuntimeError("Blender sequencer strips API is unavailable")

    if getattr(scene, "witcher_voice_replace_audio", False):
        for strip in [strip for strip in strips if strip.type == 'SOUND']:
            strips.remove(strip)
    channel = 1 if getattr(scene, "witcher_voice_replace_audio", False) else _get_next_sound_channel(scene)
    sound_path = str(sound_path)
    soundstrip = strips.new_sound(
        Path(sound_path).stem,
        sound_path,
        channel=channel,
        frame_start=int(at_frame) + 1,
    )
    soundstrip.frame_start = at_frame
    try:
        soundstrip["witcher_source_game"] = "w2"
        soundstrip["witcher_w2_voice_file"] = Path(sound_path).stem
        soundstrip[dialog_language.DIALOG_AUDIO_LANGUAGE_PROP] = _w2_voice_language_from_path(sound_path) or "en"
    except Exception:
        pass
    strip_end = int(getattr(soundstrip, "frame_final_end", 0) or 0)
    if strip_end > scene.frame_end:
        scene.frame_end = strip_end
    return soundstrip


def _import_w2_voice_pair(context, filepath, active_armature=None, use_nla=True, at_frame=None, nla_mode=None):
    dat_path, mp2_path = _resolve_w2_voice_pair(filepath, context)
    if not dat_path and not mp2_path:
        raise FileNotFoundError(f"W2 voice pair not found: {filepath}")

    _mode_map = {'REPLACE': 'replace', 'APPEND': 'append', 'APPEND_AT_CURSOR': 'append_at_cursor'}
    nla_mode = nla_mode or _mode_map.get(getattr(context.scene, 'witcher_anim_nla_mode', 'REPLACE'), 'replace')
    if at_frame is None:
        at_frame = float(context.scene.frame_current) if nla_mode == 'append_at_cursor' else 0
    else:
        at_frame = float(at_frame or 0)

    if dat_path:
        import_anims.import_lipsync(
            context,
            dat_path,
            use_NLA=use_nla,
            NLA_track="voice_import",
            override_select=active_armature,
            at_frame=at_frame,
            nla_mode=nla_mode,
        )
        if active_armature and active_armature.animation_data:
            active_armature.animation_data.use_nla = True

    soundstrip = None
    if mp2_path:
        soundstrip = _import_w2_sound_strip(context, mp2_path, at_frame=at_frame)

    return dat_path, mp2_path, soundstrip


class ImportWEM(bpy.types.Operator, ImportHelper):
    bl_idname = "witcher.import_wem"
    bl_label = "Import .wem"
    bl_options = {'PRESET', 'UNDO'}

    filename_ext = ".wem"

    filter_glob: StringProperty(
        default="*.wem",
        options={'HIDDEN'}
    )

    def execute(self, context):
        vgmstream_path = get_vgmstream_path(context)
        output_folder = get_W3_OGG_PATH(context)

        if not os.path.isfile(vgmstream_path):
            self.report({'ERROR'}, "vgmstream executable not found")
            return {'CANCELLED'}

        if not output_folder:
            output_folder = bpy.app.tempdir
            

        output_wav = os.path.join(output_folder, os.path.basename(self.filepath).replace('.wem', '.wav'))
        command = [vgmstream_path, "-o", output_wav, self.filepath]

        try:
            subprocess.run(command, check=True)
            # Here you might want to add the WAV to Blender's sequencer
        except subprocess.CalledProcessError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}

        return {'FINISHED'}

class ConvertAllWEM(bpy.types.Operator):
    """
    This will convert all unbundled .wem into .wav. It is not required as .wems will be converted when needed
    """
    bl_idname = "witcher.convert_all_wem"
    bl_label = "Conver all .wem now"
    bl_options = {'PRESET', 'UNDO'}

    def execute(self, context):
        return {'FINISHED'}

class ButtonOperatorImportVoice(bpy.types.Operator, ImportHelper):
    """Import W2/W3 lipsync and voice audio pairs"""
    bl_idname = "witcher.import_w2_voice"
    bl_label = "Import Voiceline Pair"
    filename_ext = ".dat"

    filter_glob: StringProperty(
        default="*.dat;*.mp2;*.cr2w;*.re",
        options={'HIDDEN'}
    )

    use_NLA: bpy.props.BoolProperty(name="Use NLA",
                                        default=True,
                                        description="Animation will be imported into a track called \"voice_import\" instead of action")

    def execute(self, context):
        active_armature = _get_active_armature(context)
        fdir = self.filepath
        ext = Path(fdir).suffix.lower()
        if ext in {".dat", ".mp2"} or "local_speech" in str(fdir).replace("/", "\\").lower():
            try:
                dat_path, mp2_path, soundstrip = _import_w2_voice_pair(
                    context,
                    fdir,
                    active_armature=active_armature,
                    use_nla=self.use_NLA,
                )
            except Exception as exc:
                self.report({'ERROR'}, f"W2 voice import failed: {exc}")
                return {'CANCELLED'}

            if dat_path and not active_armature:
                self.report({'WARNING'}, "No armature selected. Loaded audio, but lipsync has no character target.")
            elif dat_path and not _armature_has_face_morphs(active_armature):
                self.report({'WARNING'}, "Face morphs not loaded on the active armature.")
            if mp2_path and soundstrip is None:
                self.report({'WARNING'}, f"MP2 pair found but no sound strip was created: {mp2_path}")
            if dat_path and not mp2_path:
                self.report({'WARNING'}, f"Loaded W2 DAT only; MP2 pair not found for {Path(dat_path).stem}.")
            elif mp2_path and not dat_path:
                self.report({'WARNING'}, f"Loaded W2 MP2 only; DAT pair not found for {Path(mp2_path).stem}.")
            else:
                self.report({'INFO'}, f"W2 voice pair imported: {Path(dat_path or mp2_path).stem}")
            return {'FINISHED'}

        if (os.path.exists(fdir+'.json')):
            fdir = fdir + '.json'
        if fdir.endswith('.cr2w'):
            log.info('Importing Lipsync')
            #import_anims.import_lipsync(context, fdir)
            cr2wPath = fdir
            path = Path(cr2wPath)
            filename = Path(cr2wPath).stem
            if active_armature and active_armature.animation_data is None:
                active_armature.animation_data_create()
            _mode_map = {'REPLACE': 'replace', 'APPEND': 'append', 'APPEND_AT_CURSOR': 'append_at_cursor'}
            _nla_mode = _mode_map.get(getattr(context.scene, 'witcher_anim_nla_mode', 'REPLACE'), 'replace')
            _at_frame = float(context.scene.frame_current) if _nla_mode == 'append_at_cursor' else 0
            import_anims.import_lipsync(
                context,
                cr2wPath,
                use_NLA=self.use_NLA,
                NLA_track="voice_import",
                override_select=active_armature,
                at_frame=_at_frame,
                nla_mode=_nla_mode,
            )
            if active_armature and active_armature.animation_data:
                active_armature.animation_data.use_nla = True
            if getattr(context.scene, "witcher_voice_recreate_phonemes", False):
                if not active_armature:
                    self.report({'ERROR'}, "Recreate Phonemes failed: no character armature found. "
                                "Set a character target or select an armature.")
                    return {'CANCELLED'}
                if not _armature_has_face_morphs(active_armature):
                    self.report({'ERROR'}, "Recreate Phonemes failed: face morphs not loaded on "
                                f"'{active_armature.name}'. Load Face Morphs first (Character > Morphs), "
                                "then Create Phonemes before importing lipsync with this option.")
                    return {'CANCELLED'}
                try:
                    from .ui_voice import _recreate_phonemes_from_lipsync
                    _recreate_phonemes_from_lipsync(context, active_armature, filename, track_name="voice_import")
                except Exception as exc:
                    self.report({'ERROR'}, f"Recreate Phonemes: {exc}")
                    return {'CANCELLED'}
            soundPath = cr2wPath.replace(".cr2w", ".wav")

            speechId = None
            if path and path.parent and path.parent.name:
                speechId = path.name.split('.')[0]

            # Function to check for sound file in a given directory
            def check_sound_file(directory, suffix, speech_id):
                for file in Path(directory).glob('*'):
                    if file.suffix == suffix and speech_id in file.stem:
                        return str(file)
                return None

            if not os.path.isfile(soundPath):
                folder = path.parent.name
                if "speech." in folder and ".wem" in folder and "lipsyncanim" in filename:
                    speechId = filename.split('.')[0]
                    soundFolder = str(path.parent.parent) + "\\" + path.parent.name.replace('wem', 'wav')
                    if os.path.isdir(soundFolder):
                        # Check for both .wav and .ogg files
                        soundPath = check_sound_file(soundFolder, ".wav", speechId)
                        if not soundPath:
                            soundPath = check_sound_file(soundFolder, ".ogg", speechId)

            if not os.path.isfile(soundPath):
                sound_directory_to_check = Path(get_W3_OGG_PATH(context))
                if sound_directory_to_check.is_dir():
                    soundPath = check_sound_file(sound_directory_to_check, ".wav", speechId)
                    if not soundPath:
                        soundPath = check_sound_file(sound_directory_to_check, ".ogg", speechId)

            #search same directiory
            #search speech.en.wav
            #search defined voice dir

            if os.path.isfile(soundPath):
                log.info('Importing Sound')
                scene = context.scene

                bpy.ops.sequencer.delete()
                if not scene.sequence_editor:
                    scene.sequence_editor_create()
                from .ui_voice import _get_sequence_editor_strips
                strips = _get_sequence_editor_strips(scene.sequence_editor)
                if strips is None:
                    self.report({'ERROR'}, "Blender sequencer strips API is unavailable.")
                    return {'CANCELLED'}

                #Sequences.new_sound(name, filepath, channel, frame_start)
                soundstrip = strips.new_sound("voiceline", soundPath, 3, 0)
            if not active_armature:
                self.report({'WARNING'}, "No armature selected. Load the action onto a character after import.")
            elif not _armature_has_face_morphs(active_armature):
                self.report({'WARNING'}, "Face morphs not loaded on the active armature.")
            self.report({'INFO'}, "Lipsync import finished.")
        return {'FINISHED'}
    def invoke(self, context, event):
        if self.filepath == '':
            w2_lang = dialog_language.get_active_voice_language(context) or "en"
            w2_default = ""
            game_root = str(get_witcher2_game_path(context) or "").strip()
            if game_root:
                candidate = os.path.join(game_root, "CookedPC", "local_speech", w2_lang, "lipsync")
                if os.path.isdir(candidate):
                    w2_default = candidate
            if w2_default:
                self.filepath = w2_default
            else:
                UNCOOK_PATH = os.path.join(get_W3_VOICE_PATH(bpy.context))
                if os.path.exists(UNCOOK_PATH):
                    self.filepath = UNCOOK_PATH
        return ImportHelper.invoke(self, context, event)

class WITCHER_PT_speech_panel(WITCH_PT_Base, Panel):
    bl_idname = "WITCHER_PT_speech_panel"
    bl_parent_id = "WITCHER_PT_animset_panel"
    bl_label = "Speech & Voicelines"
    bl_description = ""
    #bl_options = {'HEADER_LAYOUT_EXPAND'}
    bl_options = {'DEFAULT_CLOSED'}

    def draw_header(self, context):
        self.layout.label(text="", icon='SPEAKER')

    @classmethod
    def poll(cls, context):
        # Speech and dialogue tools are now embedded directly in the Animation panel.
        return False

    def draw(self, context):
        """
        """
        object = context.scene
        if object == None:
            return

        layout = self.layout
        box = layout.box()
        box.label(text="Speech Tool", icon='INFO')
        info = box.column(align=True)
        info.label(text="Game Dialogue Import loads default voicelines (Reset to populate).")
        info.label(text="Import Voiceline Pair for Radish Modding Tools exports.")

        status = layout.column(align=True)
        active_armature = _get_active_armature(context)
        if active_armature:
            status.label(text=f"Active armature: {active_armature.name}", icon='ARMATURE_DATA')
            if not _armature_has_face_morphs(active_armature):
                row = status.row(align=True)
                row.alert = True
                row.label(text="Face morphs not loaded. Load them via Character > Morphs.", icon='ERROR')
        else:
            status.label(text="No armature selected. Imports still run.", icon='INFO')
            status.label(text="You'll need to load the animation onto a character.", icon='INFO')

        options = layout.row(align=True)
        if hasattr(context.scene, "witcher_voice_recreate_phonemes"):
            options.prop(context.scene, "witcher_voice_recreate_phonemes", text="Recreate Phonemes")
        if getattr(context.scene, "witcher_voice_recreate_phonemes", False):
            if hasattr(context.scene, "witcher_voice_phoneme_accuracy"):
                layout.prop(context.scene, "witcher_voice_phoneme_accuracy", text="Accuracy", slider=True)

        if hasattr(context.scene, "witcher_anim_nla_mode"):
            layout.prop(context.scene, "witcher_anim_nla_mode", text="NLA Load Mode")

        row = layout.row(align=True)
        row.operator(ButtonOperatorImportVoice.bl_idname, text="Import Voiceline Pair", icon='SPHERE')
        row.operator(ImportWEM.bl_idname, text="Import .wem", icon='SPEAKER')

class WITCHER_OT_OpenVoiceAudioPath(bpy.types.Operator):
    """Open the configured voiceline audio output folder in the OS file browser.
    You can change this path in Addon Preferences > W3_OGG_PATH."""
    bl_idname = "witcher.open_voice_audio_path"
    bl_label = "Open Voice Audio Folder"
    bl_description = (
        "Open the voiceline audio folder in the system file browser.\n"
        "Change this path in Addon Preferences (W3_OGG_PATH / W3_VOICE_PATH)."
    )

    def execute(self, context):
        from .. import get_W3_VOICE_PATH
        path = bpy.path.abspath(get_W3_VOICE_PATH(context))
        if not path or not os.path.isdir(path):
            self.report({'WARNING'}, f"Voice path not found: {path or '(not set in Addon Preferences)'}")
            return {'CANCELLED'}
        try:
            bpy.ops.wm.path_open(filepath=path)
        except Exception as e:
            self.report({'ERROR'}, f"Could not open folder: {e}")
            return {'CANCELLED'}
        return {'FINISHED'}

class UnbundleSpeechOperator(bpy.types.Operator):
    bl_idname = "witcher.unbundle_speech"
    bl_label = "Unbundle Lipsync (.cr2w, wem) now"

    def execute(self, context):
        game_path = bpy.path.abspath(get_game_path(context))
        if not game_path or not os.path.isdir(game_path):
            self.report({'ERROR'}, "Witcher 3 path not set or invalid.")
            return {'CANCELLED'}

        content_dir = os.path.join(game_path, "content")
        if not os.path.isdir(content_dir):
            self.report({'ERROR'}, f"Invalid Witcher 3 path (missing 'content' folder): {game_path}")
            return {'CANCELLED'}

        voice_path = bpy.path.abspath(get_W3_VOICE_PATH(context))
        if not voice_path:
            self.report({'ERROR'}, "Unbundled lipsync path not set.")
            return {'CANCELLED'}

        try:
            os.makedirs(voice_path, exist_ok=True)
        except Exception as exc:
            self.report({'ERROR'}, f"Unable to create output folder: {exc}")
            return {'CANCELLED'}

        try:
            speech_manager = LoadSpeechManager()
        except Exception as exc:
            self.report({'ERROR'}, f"Failed to load speech cache: {exc}")
            return {'CANCELLED'}

        total = len(speech_manager.Items)
        if total == 0:
            self.report({'WARNING'}, "No speech entries found.")
            return {'CANCELLED'}

        wm = context.window_manager
        workspace = getattr(context, "workspace", None)
        update_every = max(1, total // 100)
        extracted = 0
        skipped = 0
        failed = 0

        log.info("Unbundling lipsync to: %s", voice_path)
        if wm:
            wm.progress_begin(0, total)
        try:
            for idx, entries in enumerate(speech_manager.Items.values()):
                if not entries:
                    continue
                entry = entries[0]
                entry_id = str(entry.id)
                base_name = pad_filename(entry_id)
                cr2w_path = os.path.join(voice_path, f"{base_name}.cr2w")
                wem_path = os.path.join(voice_path, f"{base_name}.wem")

                if os.path.isfile(cr2w_path) and os.path.isfile(wem_path):
                    skipped += 1
                else:
                    try:
                        entry.extract_to_file(entry_id)
                        extracted += 1
                    except Exception as exc:
                        failed += 1
                        log.warning("Failed to unbundle speech %s: %s", entry_id, exc)

                if (idx % update_every == 0) or (idx + 1 == total):
                    if wm:
                        wm.progress_update(idx + 1)
                    if workspace:
                        percent = int(round(((idx + 1) / total) * 100))
                        workspace.status_text_set(
                            f"Unbundling lipsync... {percent}% ({idx + 1}/{total})"
                        )
        finally:
            if wm:
                wm.progress_end()
            if workspace:
                workspace.status_text_set(None)

        _update_speech_counts(context.scene, voice_path, total_pairs=total)
        self.report(
            {'INFO'},
            f"Unbundle complete. Extracted: {extracted}, skipped: {skipped}, failed: {failed}.",
        )
        return {'FINISHED'}


def _count_extracted_pairs(voice_path: str) -> tuple[int, int, int]:
    if not voice_path or not os.path.isdir(voice_path):
        return 0, 0, 0
    cr2w_files = {path.stem for path in Path(voice_path).glob("*.cr2w")}
    wem_files = {path.stem for path in Path(voice_path).glob("*.wem")}
    pairs = cr2w_files.intersection(wem_files)
    return len(pairs), len(cr2w_files), len(wem_files)


def _update_speech_counts(scene, voice_path: str, total_pairs: int | None = None) -> None:
    pair_count, cr2w_count, wem_count = _count_extracted_pairs(voice_path)
    if total_pairs is not None:
        scene.witcher_speech_pair_total = total_pairs
    scene.witcher_speech_pair_extracted = pair_count
    scene.witcher_speech_pair_cr2w = cr2w_count
    scene.witcher_speech_pair_wem = wem_count
    scene.witcher_speech_pair_last_refresh = time.strftime("%Y-%m-%d %H:%M:%S")


class RefreshSpeechCountsOperator(bpy.types.Operator):
    bl_idname = "witcher.refresh_speech_counts"
    bl_label = "Refresh Speech Counts"
    bl_description = "Re-scan the speech cache folder and update the cache count values shown in Cache Tools"
    bl_options = {'INTERNAL'}

    @classmethod
    def description(cls, context, properties):
        scene = getattr(context, "scene", None)
        if scene is None:
            return cls.bl_description
        extracted = int(getattr(scene, "witcher_speech_pair_extracted", 0))
        cr2w = int(getattr(scene, "witcher_speech_pair_cr2w", 0))
        wem = int(getattr(scene, "witcher_speech_pair_wem", 0))
        return (
            f"Re-scan cache files and refresh counts. "
            f"Current: {extracted} pairs, {cr2w} .cr2w, {wem} .wem."
        )

    def execute(self, context):
        voice_path = bpy.path.abspath(get_W3_VOICE_PATH(context))
        total_pairs = 0
        try:
            speech_manager = LoadSpeechManager()
            total_pairs = len(speech_manager.Items)
        except Exception as exc:
            self.report({'WARNING'}, f"Failed to load speech cache: {exc}")
        _update_speech_counts(context.scene, voice_path, total_pairs=total_pairs)
        self.report({'INFO'}, "Speech counts refreshed.")
        return {'FINISHED'}

class SCENE_PT_speech_settings(WITCH_PT_Base, bpy.types.Panel):
    bl_parent_id = "WITCHER_PT_speech_panel"

    bl_label = "Speech Cache / Paths"
    bl_idname = "SCENE_PT_speech_settings"

    def draw_header(self, context):
        self.layout.label(text="", icon='FILE_FOLDER')

    def draw(self, context):
        layout = self.layout
        addon_prefs = get_all_addon_prefs(context)
        scene = context.scene

        # Add UI elements for editing preferences
        layout.label(text = '<< Path Settings >>')
        layout.prop(addon_prefs, "witcher_game_path")
        layout.prop(addon_prefs, "W3_VOICE_PATH")
        layout.operator(UnbundleSpeechOperator.bl_idname, icon='SPHERE')
        counts_box = layout.box()
        counts_box.label(text="Speech Counts", icon='INFO')
        counts_box.label(text=f"Bundle pairs: {scene.witcher_speech_pair_total}")
        counts_box.label(text=f"Extracted pairs: {scene.witcher_speech_pair_extracted}")
        counts_box.label(text=f".cr2w files: {scene.witcher_speech_pair_cr2w}")
        counts_box.label(text=f".wem files: {scene.witcher_speech_pair_wem}")
        if scene.witcher_speech_pair_last_refresh:
            counts_box.label(text=f"Last refresh: {scene.witcher_speech_pair_last_refresh}")
        counts_box.operator(RefreshSpeechCountsOperator.bl_idname, icon='FILE_REFRESH')
        layout.operator(ConvertAllWEM.bl_idname)
        vgmstream_path = get_vgmstream_path(context)
        vgmstream_exists = os.path.isfile(vgmstream_path)
        layout.label(
            text=f"vgmstream: bundled ({'found' if vgmstream_exists else 'missing'})",
            icon='CHECKMARK' if vgmstream_exists else 'ERROR'
        )

classes = [
    UnbundleSpeechOperator,
    RefreshSpeechCountsOperator,
    ImportWEM,
    ConvertAllWEM,
    ButtonOperatorImportVoice,
    WITCHER_OT_OpenVoiceAudioPath,
    WITCHER_PT_speech_panel,
    SCENE_PT_speech_settings,
]

def register():
    bpy.types.Scene.witcher_speech_pair_total = IntProperty(
        name="Speech Pairs (Bundle)",
        default=0,
    )
    bpy.types.Scene.witcher_speech_pair_extracted = IntProperty(
        name="Speech Pairs (Extracted)",
        default=0,
    )
    bpy.types.Scene.witcher_speech_pair_cr2w = IntProperty(
        name="Speech Files (.cr2w)",
        default=0,
    )
    bpy.types.Scene.witcher_speech_pair_wem = IntProperty(
        name="Speech Files (.wem)",
        default=0,
    )
    bpy.types.Scene.witcher_speech_pair_last_refresh = StringProperty(
        name="Speech Counts Last Refresh",
        default="",
    )
    for c in classes:
        bpy.utils.register_class(c)

def unregister():
    for prop_name in (
        "witcher_speech_pair_total",
        "witcher_speech_pair_extracted",
        "witcher_speech_pair_cr2w",
        "witcher_speech_pair_wem",
        "witcher_speech_pair_last_refresh",
    ):
        if hasattr(bpy.types.Scene, prop_name):
            delattr(bpy.types.Scene, prop_name)
    for c in reversed(classes):
        bpy.utils.unregister_class(c)

if __name__ == '__main__':
    register()

