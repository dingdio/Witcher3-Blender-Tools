
import logging
import os
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
)
from ..CR2W.witcher_cache.Speech import LoadSpeechManager
from ..CR2W.witcher_cache.Speech.W3Speech import pad_filename
from ..importers import import_anims, import_rig
from ..exporters import export_anims
from ..ui.ui_utils import WITCH_PT_Base

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
    return bool(armature and armature.pose and "w3_face_poses" in armature.pose.bones)

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
    """Import W2 lipsync Animation"""
    bl_idname = "witcher.import_w2_voice"
    bl_label = "w2 lipsync"
    filename_ext = ".cr2w"

    use_NLA: bpy.props.BoolProperty(name="Use NLA",
                                        default=True,
                                        description="Animation will be imported into a track called \"voice_import\" instead of action")

    def execute(self, context):
        active_armature = _get_active_armature(context)
        fdir = self.filepath
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
            import_anims.import_lipsync(
                context,
                cr2wPath,
                use_NLA=self.use_NLA,
                NLA_track="voice_import",
                override_select=active_armature
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
        UNCOOK_PATH = os.path.join(get_W3_VOICE_PATH(bpy.context))
        if os.path.exists(UNCOOK_PATH):
            self.filepath = UNCOOK_PATH if self.filepath == '' else self.filepath
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

