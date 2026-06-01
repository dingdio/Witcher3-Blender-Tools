import logging
from pathlib import Path
from ..importers import import_anims
log = logging.getLogger(__name__)
from .. import get_uncook_path, get_w2_unbundle_path, get_W3_VOICE_PATH, get_W3_OGG_PATH, get_vgmstream_path, get_all_addon_prefs
from ..extension_paths import get_cache_root, get_dev_override, get_dev_override_list
from ..action_compat import bind_strip_action_slot, new_action_fcurve, resolve_action_slot
from ..CR2W.witcher_cache.Speech import LoadSpeechManager
from ..CR2W.witcher_cache.Speech.W3Speech import SpeechEntry
from ..CR2W.witcher_cache.W3Strings import LoadStringsManager
from .. import dialogue_browser_core as browser_core
from . import phoneme_helper
from .ui_morphs import get_face_meshs

import csv
import json
import os
import bpy
import math
import subprocess
import shutil
import time
import numpy as np

from bpy.props import (
    IntProperty,
    BoolProperty,
    EnumProperty,
    StringProperty,
)
from .. import dialog_language

VOICE_LIST_PROP = "witcher_voice_list"
VOICE_LIST_INDEX_PROP = "witcher_voice_list_index"
VOICE_GAME_W3 = "W3"
VOICE_GAME_W2 = "W2"
VOICE_CACHE_VERSION = 16

_voice_node_cache = []   # list[dict] — the full 64k voice line dataset
_voice_cache_loaded = False
_voice_filtered_indices = []
_VOICE_LIST_DEFERRED = False
_VOICE_BROWSER_INDEX_LANGUAGE = "en"
_voice_cache_identity_loaded = None


def _voice_browser_index_language():
    return _VOICE_BROWSER_INDEX_LANGUAGE


def get_active_voice_game(context=None):
    scene = getattr(context, "scene", None) if context is not None else None
    if scene is None and hasattr(context, "witcher_voice_game"):
        scene = context
    if scene is None:
        try:
            scene = bpy.context.scene
        except Exception:
            scene = None
    game = getattr(scene, "witcher_voice_game", VOICE_GAME_W3) if scene is not None else VOICE_GAME_W3
    return VOICE_GAME_W2 if str(game or "").upper() == VOICE_GAME_W2 else VOICE_GAME_W3


def is_witcher2_voice_browser(context=None):
    return get_active_voice_game(context) == VOICE_GAME_W2


def _voice_cache_identity(context=None):
    game = get_active_voice_game(context)
    text_language = dialog_language.get_active_text_language(context)
    if game == VOICE_GAME_W2:
        index_language = dialog_language.get_active_voice_language(context)
    else:
        index_language = _voice_browser_index_language()
    index_language = dialog_language.normalize_dialog_language(index_language or "en")
    text_language = dialog_language.normalize_dialog_language(text_language or "en")
    return game, index_language, text_language

def _voice_cache_path(context=None):
    """Return the writable user-cache path for the voice cache JSON file."""
    cache_dir = Path(get_cache_root(create=True)) / "Voice"
    cache_dir.mkdir(parents=True, exist_ok=True)
    game, index_language, text_language = _voice_cache_identity(context)
    return str(cache_dir / f"voice_cache_v{VOICE_CACHE_VERSION}_{game.lower()}_index_{index_language}_{text_language}.json")

def _save_voice_cache(context=None):
    """Write _voice_node_cache to disk as JSON."""
    global _voice_node_cache
    game, index_language, text_language = _voice_cache_identity(context)
    path = _voice_cache_path(context)
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({
                'version': VOICE_CACHE_VERSION,
                'game': game,
                'text_language': text_language,
                'index_language': index_language,
                'count': len(_voice_node_cache),
                'nodes': _voice_node_cache,
            }, f, ensure_ascii=False, separators=(',', ':'))
        log.info("Voice cache saved: %d items → %s", len(_voice_node_cache), path)
    except Exception as exc:
        log.error("Failed to save voice cache: %s", exc)

def _load_voice_cache(context=None):
    """Load _voice_node_cache from the JSON file on disk. Returns True if loaded."""
    global _voice_node_cache, _voice_cache_loaded, _voice_filtered_indices, _voice_cache_identity_loaded
    identity = _voice_cache_identity(context)
    path = _voice_cache_path(context)
    if not os.path.isfile(path):
        return False
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get('nodes'), list):
            if int(data.get('version') or 0) < VOICE_CACHE_VERSION:
                return False
            if str(data.get('game') or VOICE_GAME_W3).upper() != identity[0]:
                return False
            _voice_node_cache = data['nodes']
            if not _voice_node_cache:
                _voice_cache_loaded = False
                _voice_filtered_indices = []
                _voice_cache_identity_loaded = None
                return False
            _voice_cache_loaded = True
            _voice_cache_identity_loaded = identity
            _voice_filtered_indices = list(range(len(_voice_node_cache)))
            _refresh_speaker_stats(_voice_node_cache)
            log.info("Voice cache loaded: %d items from %s", len(_voice_node_cache), path)
            return True
    except Exception as exc:
        log.error("Failed to load voice cache: %s", exc)
    return False

def _cache_is_stale(context=None):
    """Check if the cached data is stale compared to the speech manager."""
    try:
        if get_active_voice_game(context) == VOICE_GAME_W2:
            from ..CR2W.witcher_cache.W2Speech import LoadWitcher2SpeechManager

            speech_manager = LoadWitcher2SpeechManager(
                language=dialog_language.get_active_voice_language(context)
            )
        else:
            speech_manager = LoadSpeechManager(language=_voice_browser_index_language())
        live_count = len(speech_manager.Items)
        cached_count = len(_voice_node_cache)
        if cached_count == 0:
            return True
        return live_count != cached_count
    except Exception:
        return False  # Can't check — assume cache is OK

def get_voice_node_count():
    """Public helper: return the number of voice nodes in the cache."""
    return len(_voice_node_cache)

def ensure_voice_cache(context=None):
    """Load the voice cache from disk, rebuilding when the cached language list is empty."""
    global _voice_cache_loaded, _voice_node_cache, _voice_filtered_indices, _voice_cache_identity_loaded
    identity = _voice_cache_identity(context)
    if _voice_cache_loaded and _voice_node_cache and _voice_cache_identity_loaded == identity:
        return
    if _voice_cache_identity_loaded != identity:
        _voice_node_cache = []
        _voice_filtered_indices = []
        _voice_cache_loaded = False
        _voice_cache_identity_loaded = None
    if not _load_voice_cache(context) and not _voice_node_cache:
        SetupNodeData(do_reload_strings=False)


def _deferred_apply_voice_filter():
    global _VOICE_LIST_DEFERRED
    _VOICE_LIST_DEFERRED = False
    try:
        _apply_voice_filter(bpy.context)
    except Exception:
        log.warning("Deferred voice filter apply failed.", exc_info=True)
    return None


def _schedule_deferred_voice_filter():
    global _VOICE_LIST_DEFERRED
    if _VOICE_LIST_DEFERRED:
        return
    _VOICE_LIST_DEFERRED = True
    try:
        bpy.app.timers.register(_deferred_apply_voice_filter, first_interval=0.0)
    except Exception:
        _VOICE_LIST_DEFERRED = False
        log.warning("Unable to register deferred voice filter timer.", exc_info=True)


def ensure_voice_list_initialized(context):
    """Load cache from disk and auto-populate the voice list on first panel access."""
    scene = getattr(context, "scene", None)
    if scene is None:
        return
    ensure_voice_cache(context)
    items = getattr(scene, VOICE_LIST_PROP, None)
    if items is None or len(items) > 0:
        return
    if _voice_node_cache:
        _schedule_deferred_voice_filter()

def _get_active_armature(context):
    """Resolve the best armature for voice import and phoneme recreation."""
    return _resolve_voice_target_armature(context)

def _armature_has_face_morphs(armature):
    return bool(
        armature
        and armature.pose
        and (
            "w3_face_poses" in armature.pose.bones
            or "w2_face_poses" in armature.pose.bones
        )
    )


def _object_to_armature(obj):
    if obj and getattr(obj, "type", None) == 'ARMATURE':
        return obj
    if obj and getattr(obj, "type", None) == 'MESH':
        parent = getattr(obj, "parent", None)
        if parent and getattr(parent, "type", None) == 'ARMATURE':
            return parent
    return None


def _resolve_voice_target_armature(context, actor=None):
    actor_armature = _object_to_armature(actor)
    if actor_armature:
        return actor_armature

    from .armature_context import get_main_armature

    armature = get_main_armature(context, prefer_active=True, remember=False, fallback=True)
    if armature:
        return armature

    active_obj = getattr(context, "active_object", None)
    active_armature = _object_to_armature(active_obj)
    if active_armature:
        return active_armature

    for obj in getattr(context, "selected_objects", []) or []:
        selected_armature = _object_to_armature(obj)
        if selected_armature:
            return selected_armature
    return None


def _auto_load_face_morphs(context, armature):
    if not armature or _armature_has_face_morphs(armature):
        return
    try:
        from .ui_mimics import _ensure_face_morphs_loaded

        _ensure_face_morphs_loaded(context, armature)
    except Exception as exc:
        log.warning("Auto face morph load failed: %s", exc)

def _find_face_meshes(context, armature):
    scene = context.scene
    face_mesh_objs = []
    face_rig_name = armature.get('mimicFace') if armature else None
    if face_rig_name:
        face_meshes, _face_arms = get_face_meshs(face_rig_name)
        for mesh_name in face_meshes:
            mesh_obj = scene.objects.get(mesh_name)
            if mesh_obj and mesh_obj.type == 'MESH':
                face_mesh_objs.append(mesh_obj)
    if face_mesh_objs:
        return face_mesh_objs

    for obj in scene.objects:
        if obj.type != 'MESH':
            continue
        if obj.parent == armature:
            face_mesh_objs.append(obj)
            continue
        for mod in obj.modifiers:
            if mod.type == 'ARMATURE' and mod.object == armature:
                face_mesh_objs.append(obj)
                break
    return face_mesh_objs

def _get_anim_data_range(anim_data, track_name="voice_import"):
    if not anim_data:
        return None, None
    for track in anim_data.nla_tracks:
        if track.name != track_name:
            continue
        strips = list(track.strips)
        if strips:
            start = min(strip.frame_start for strip in strips)
            end = max(strip.frame_end for strip in strips)
            return int(math.floor(start)), int(math.ceil(end))
    action = anim_data.action
    if action:
        start, end = action.frame_range
        return int(math.floor(start)), int(math.ceil(end))
    return None, None

def _get_lipsync_range(shape_keys, armature, track_name="voice_import"):
    if shape_keys and shape_keys.animation_data:
        start, end = _get_anim_data_range(shape_keys.animation_data, track_name=track_name)
        if start is not None and end is not None:
            return start, end
    if armature and armature.animation_data:
        return _get_anim_data_range(armature.animation_data, track_name=track_name)
    return None, None

def _sample_morph_values(context, frames, pose_bone, key_blocks, morph_names, prefer_pose=False):
    values = np.zeros((len(frames), len(morph_names)), dtype=np.float32)
    if not frames:
        return values
    prev_frame = context.scene.frame_current
    try:
        for idx, frame in enumerate(frames):
            context.scene.frame_set(frame)
            for midx, morph_name in enumerate(morph_names):
                if prefer_pose and pose_bone and morph_name in pose_bone:
                    values[idx, midx] = float(pose_bone[morph_name])
                elif key_blocks and morph_name in key_blocks:
                    values[idx, midx] = float(key_blocks[morph_name].value)
                elif pose_bone and morph_name in pose_bone:
                    values[idx, midx] = float(pose_bone[morph_name])
    finally:
        context.scene.frame_set(prev_frame)
    return values

def _build_phoneme_solver(ref_mesh, morph_list, morphs_data, phoneme_list):
    key_blocks = ref_mesh.data.shape_keys.key_blocks
    used_morphs = [morph for morph in morph_list if morph in key_blocks]
    if not used_morphs:
        return None, None
    weight_matrix = np.zeros((len(used_morphs), len(phoneme_list)), dtype=np.float32)
    for i, morph_name in enumerate(used_morphs):
        weights = morphs_data.get(morph_name, {})
        for j, phoneme in enumerate(phoneme_list):
            weight_matrix[i, j] = float(weights.get(phoneme, 0.0))
    return used_morphs, weight_matrix


def _nnls_solve(weight_matrix, morph_values_T, max_iter=200, tol=1e-4):
    """Non-negative least squares via multiplicative updates.

    Solves  weight_matrix @ H ≈ morph_values_T  subject to  H >= 0.

    Regular lstsq can produce negative phoneme values that get clipped to 0,
    breaking the balance and causing morphs to overshoot.  NNLS finds the best
    solution that is non-negative from the start, so the reconstruction through
    the driver weight matrix stays accurate.
    """
    eps = 1e-8
    W = weight_matrix                       # (num_morphs, num_phonemes)
    V = np.maximum(0.0, morph_values_T)     # (num_morphs, num_frames)

    # Seed from clipped lstsq for fast convergence
    H = np.maximum(eps, np.linalg.lstsq(W, V, rcond=None)[0])  # (num_phonemes, num_frames)

    WtW = W.T @ W   # (num_phonemes, num_phonemes)
    WtV = W.T @ V   # (num_phonemes, num_frames)

    for _i in range(max_iter):
        H_prev = H.copy()
        denom = WtW @ H + eps
        H = H * (WtV / denom)
        if np.max(np.abs(H - H_prev)) < tol:
            break

    return np.clip(H, 0.0, 1.0)

def _sparsify_phonemes(solved, min_value=0.05, blend_frames=2, accuracy=0.0):
    """Sparsify solved phoneme values based on *accuracy*.

    *accuracy* controls how many phonemes are kept per frame:
      - **0.0** (default): winner-takes-all — only the strongest phoneme per
        frame is kept.  Clean, but loses subtle blends.
      - **1.0**: full accuracy — all solved values above *min_value* are kept
        unchanged, reproducing the original morph mix as closely as possible.
      - **0.0 < accuracy < 1.0**: keep the top-N phonemes per frame where N
        scales between 1 and the total phoneme count.  Higher values keep more
        simultaneous phonemes for better reproduction of the original morphs.

    Crossfade blending is applied for values below 1.0 to smooth transitions.
    """
    num_phonemes, num_frames = solved.shape
    accuracy = max(0.0, min(1.0, accuracy))

    # Full accuracy — keep everything above the noise floor.
    if accuracy >= 1.0:
        result = solved.copy()
        result[result < min_value] = 0.0
        return result

    # How many phonemes to keep per frame (1 at accuracy=0, all at accuracy→1).
    max_active = max(1, int(round(1 + (num_phonemes - 1) * accuracy)))

    result = np.zeros_like(solved)

    # Pass 1: keep top-N phonemes per frame
    for k in range(num_frames):
        col = solved[:, k].copy()
        # Sort indices by value descending
        order = np.argsort(col)[::-1]
        for rank, idx in enumerate(order):
            if rank >= max_active:
                break
            val = float(col[idx])
            if val > min_value:
                result[idx, k] = val

    # Pass 2: crossfade at transitions (only when sparsifying, i.e. max_active < num_phonemes)
    if max_active == 1:
        # Winner-takes-all path: apply crossfades at winner changes
        winners = np.full(num_frames, -1, dtype=int)
        winner_vals = np.zeros(num_frames, dtype=np.float32)
        for k in range(num_frames):
            idx = int(np.argmax(result[:, k]))
            val = float(result[idx, k])
            if val > min_value:
                winners[k] = idx
                winner_vals[k] = val

        for k in range(1, num_frames):
            if winners[k] != winners[k - 1]:
                out_ph = winners[k - 1]
                in_ph = winners[k]
                for b in range(1, blend_frames + 1):
                    fk = k - 1 + b
                    if fk >= num_frames:
                        break
                    t = b / (blend_frames + 1)
                    if out_ph >= 0:
                        result[out_ph, fk] = max(result[out_ph, fk], winner_vals[k - 1] * (1.0 - t))
                    if in_ph >= 0:
                        result[in_ph, fk] = max(result[in_ph, fk], winner_vals[k] * t)

    return result


def _compress_to_keyframes(values_row, frames, eps=1e-4):
    """Return (frame, value) pairs only where the value actually changes.

    Always includes the first and last frame so the strip has well-defined
    bounds. All interior frames where the value is constant are skipped,
    giving long flat holds with very few keyframes.
    """
    pts = []
    n = len(frames)
    for i in range(n):
        val = float(values_row[i])
        prev_val = float(values_row[i - 1]) if i > 0 else val
        next_val = float(values_row[i + 1]) if i < n - 1 else val
        if i == 0 or i == n - 1 or abs(val - prev_val) > eps or abs(val - next_val) > eps:
            pts.append((frames[i], val))
    return pts


def _track_name_matches(track, track_name):
    if track is None:
        return False
    current_name = str(getattr(track, "name", "") or "")
    return current_name == track_name or current_name.startswith(f"{track_name}.")


def _remove_nla_tracks(anim_data, track_name):
    removed_actions = []
    if anim_data is None:
        return removed_actions

    for track in list(anim_data.nla_tracks):
        if not _track_name_matches(track, track_name):
            continue
        for strip in list(track.strips):
            action = getattr(strip, "action", None)
            if action and action.name not in removed_actions:
                removed_actions.append(action.name)
            track.strips.remove(strip)
        anim_data.nla_tracks.remove(track)
    return removed_actions


def _remove_orphan_actions(action_names):
    for action_name in action_names or []:
        action = bpy.data.actions.get(action_name)
        if action and action.users == 0:
            bpy.data.actions.remove(action)


def _apply_phoneme_action(armature, pose_bone, phoneme_list, frames, phoneme_values, action_name, track_name="voice_import_phoneme"):
    if armature.animation_data is None:
        armature.animation_data_create()
    armature.animation_data.use_nla = True

    old_actions = _remove_nla_tracks(armature.animation_data, track_name)

    action = bpy.data.actions.new(name=action_name)

    for idx, phoneme in enumerate(phoneme_list):
        if phoneme not in pose_bone:
            pose_bone[phoneme] = 0.0
        prop_ui = pose_bone.id_properties_ui(phoneme)
        prop_ui.update(min=0.0, max=1.0)

        data_path = f'pose.bones["{pose_bone.name}"]["{phoneme}"]'
        pts = _compress_to_keyframes(phoneme_values[idx], frames)
        if not pts:
            continue
        fcurve = new_action_fcurve(action, armature, data_path=data_path)
        fcurve.keyframe_points.add(len(pts))
        for ki, (fr, val) in enumerate(pts):
            fcurve.keyframe_points[ki].co = (fr, val)
            fcurve.keyframe_points[ki].interpolation = 'LINEAR'

    track = armature.animation_data.nla_tracks.new()
    track.name = track_name

    if frames:
        start_frame = frames[0]
        end_frame = frames[-1] + 1
        strip = track.strips.new(action.name, int(start_frame), action)
        bind_strip_action_slot(strip, resolve_action_slot(action, target=armature, ensure=True))
        strip.frame_start = start_frame
        strip.frame_end = end_frame
        strip.blend_type = 'REPLACE'

    _remove_orphan_actions(old_actions)

def _remove_lipsync_tracks(meshes, armature=None, track_name="voice_import"):
    """Delete the raw lipsync NLA tracks after phoneme solve.

    When phonemes are active the face is driven entirely by phoneme properties
    through shape key drivers.  The raw morph NLA track is no longer needed and
    must be removed so it does not compete with or override the phoneme system.
    """
    for mesh_obj in meshes:
        shape_keys = getattr(mesh_obj.data, "shape_keys", None)
        if not shape_keys or not shape_keys.animation_data:
            continue
        old_actions = _remove_nla_tracks(shape_keys.animation_data, track_name)
        _remove_orphan_actions(old_actions)
    if armature and armature.animation_data:
        old_actions = _remove_nla_tracks(armature.animation_data, track_name)
        _remove_orphan_actions(old_actions)

def _recreate_phonemes_from_lipsync(context, armature, voice_id, track_name="voice_import"):
    """Solve phoneme curves from imported lipsync morph animation.

    Returns True on success, or raises RuntimeError with a user-readable
    message explaining why it failed.
    """
    if not armature:
        raise RuntimeError("No armature provided.")
    if not _armature_has_face_morphs(armature):
        raise RuntimeError(
            f"Face morphs not loaded on '{armature.name}'. "
            "Load Face Morphs first, then Create Phonemes (Character > Morphs)."
        )

    try:
        _phonemes_data, morphs_data, phoneme_list, morph_list = phoneme_helper.read_phoneme_weights()
    except Exception as exc:
        raise RuntimeError(f"Failed to read phonemes.txt: {exc}") from exc

    face_meshes = _find_face_meshes(context, armature)
    if not face_meshes:
        raise RuntimeError(
            f"No face meshes found for '{armature.name}'. "
            "Ensure the character has face meshes with an armature modifier."
        )

    ref_mesh = next((mesh for mesh in face_meshes if mesh.data.shape_keys), None)
    if ref_mesh is None or ref_mesh.data.shape_keys is None:
        raise RuntimeError(
            "Face meshes have no shape keys. "
            "Run Create Phonemes (Character > Morphs) to set up shape keys and drivers first."
        )

    shape_keys = ref_mesh.data.shape_keys
    start_frame, end_frame = _get_lipsync_range(shape_keys, armature, track_name=track_name)
    if start_frame is None or end_frame is None:
        raise RuntimeError(
            "Lipsync animation range not found. "
            "The lipsync import may have failed or produced no keyframes."
        )

    used_morphs, weight_matrix = _build_phoneme_solver(ref_mesh, morph_list, morphs_data, phoneme_list)
    if weight_matrix is None or weight_matrix.size == 0:
        raise RuntimeError(
            "No morph weights available for the phoneme solver. "
            "Ensure phonemes.txt is present and face mesh shape keys match the expected morph names."
        )

    frames = list(range(start_frame, end_frame + 1))
    num_frames = len(frames)
    key_blocks = shape_keys.key_blocks
    pose_bone = armature.pose.bones.get("w3_face_poses")

    # Ensure NLA evaluates during frame_set() sampling — without this the
    # voice_import NLA track is ignored and all morph values are sampled as 0.
    if armature.animation_data:
        armature.animation_data.use_nla = True

    morph_values_pose = _sample_morph_values(context, frames, pose_bone, key_blocks, used_morphs, prefer_pose=True)
    morph_values_keys = _sample_morph_values(context, frames, pose_bone, key_blocks, used_morphs, prefer_pose=False)
    pose_score = float(np.sum(np.abs(morph_values_pose)))
    key_score = float(np.sum(np.abs(morph_values_keys)))

    if pose_score >= key_score:
        morph_values = morph_values_pose
        log.info("Phoneme recreation: using pose bone morph values.")
    else:
        morph_values = morph_values_keys
        log.info("Phoneme recreation: using mesh shape key values.")

    try:
        solved = _nnls_solve(weight_matrix, morph_values.T)
    except Exception as exc:
        raise RuntimeError(f"Phoneme solve failed: {exc}") from exc

    accuracy = getattr(context.scene, "witcher_voice_phoneme_accuracy", 0.5)
    solved = _sparsify_phonemes(solved, accuracy=accuracy)

    pose_bone = armature.pose.bones.get("w3_face_poses")
    if pose_bone is None:
        raise RuntimeError(
            f"Missing 'w3_face_poses' bone on '{armature.name}'. "
            "Run Create Phonemes (Character > Morphs) first."
        )

    action_name = f"{voice_id}_phonemes"
    _apply_phoneme_action(armature, pose_bone, phoneme_list, frames, solved, action_name)
    _remove_lipsync_tracks(face_meshes, armature=armature, track_name=track_name)

    rig_settings = getattr(armature.data, "witcherui_RigSettings", None)
    if rig_settings and not getattr(rig_settings, "phoneme_enabled", True):
        rig_settings.phoneme_enabled = True
    return True

class VoiceLineResourceManager:
    resourceManager = None
    def __init__(self):
        
        RES_DIR = Path(__file__)
        RES_DIR = str(Path(RES_DIR).parents[1])
        filename = os.path.join(RES_DIR, "CR2W\\data\\actor_voicelines.csv")
        self.pathashespath = filename
        #self.HashdumpDict = {}
        reader = csv.DictReader(open(self.pathashespath), delimiter=";")
        
        self.HashdumpDict = {}
        self.SpeakerById = {}
        for row in reader:
            self.HashdumpDict[row["ID"]] = row["CAT1"]+" "+row["CAT2"]+" "+row["CAT3"]+": "+row["Caption"]+" "+row["duration"]
            speaker_name = _fallback_speaker_from_csv_row(row)
            if speaker_name:
                self.SpeakerById[row["ID"]] = speaker_name
    @staticmethod
    def Get():
        if (VoiceLineResourceManager.resourceManager == None):
            VoiceLineResourceManager.resourceManager = VoiceLineResourceManager();
        return VoiceLineResourceManager.resourceManager;




def _make_voice_node(*, name="", selfIndex=-1, parentIndex=-1, childCount=0,
                      voiceLineId="0000000000", speaker="", line_id="",
                      duration="", text="", display_full="",
                      display_compact="", search_blob="", text_language="",
                      voice_language="", game=VOICE_GAME_W3, source_path="",
                      speaker_candidates=None, scene_path="", source_scenes=None, entity_path=""):
    """Create a voice node dict (replaces the old MyVoiceListNode PropertyGroup)."""
    return {
        'game': game,
        'name': name,
        'selfIndex': selfIndex,
        'parentIndex': parentIndex,
        'childCount': childCount,
        'voiceLineId': voiceLineId,
        'speaker': speaker,
        'line_id': line_id,
        'duration': duration,
        'text': text,
        'display_full': display_full,
        'display_compact': display_compact,
        'search_blob': search_blob,
        'text_language': text_language,
        'voice_language': voice_language,
        'source_path': source_path,
        'speaker_candidates': speaker_candidates or [],
        'scene_path': scene_path,
        'source_scenes': source_scenes or ([scene_path] if scene_path else []),
        'entity_path': entity_path,
    }

class MyVoiceListItem(bpy.types.PropertyGroup):
    game: bpy.props.StringProperty(default=VOICE_GAME_W3)
    indent: bpy.props.IntProperty(default=0)
    expanded: bpy.props.BoolProperty(default=False)
    nodeIndex : bpy.props.IntProperty(default=-1) #index into the real tree data.
    childCount: bpy.props.IntProperty(default=0) #should equal myNodes[nodeIndex].childCount
    voiceLineId: bpy.props.StringProperty(default="0000000000")
    speaker: bpy.props.StringProperty(default="")
    line_id: bpy.props.StringProperty(default="")
    duration: bpy.props.StringProperty(default="")
    text: bpy.props.StringProperty(default="")
    display_full: bpy.props.StringProperty(default="")
    display_compact: bpy.props.StringProperty(default="")
    source_path: bpy.props.StringProperty(default="")
    speaker_candidates: bpy.props.StringProperty(default="")
    scene_path: bpy.props.StringProperty(default="")
    entity_path: bpy.props.StringProperty(default="")


class VoiceAssociatedPathItem(bpy.types.PropertyGroup):
    kind: bpy.props.StringProperty(default="")
    game: bpy.props.StringProperty(default=VOICE_GAME_W3)
    label: bpy.props.StringProperty(default="")
    source: bpy.props.StringProperty(default="")
    repo_path: bpy.props.StringProperty(
        name="Repo Path",
        default="",
        description="Game-relative path associated with the selected dialogue line",
    )
    resolved_path: bpy.props.StringProperty(
        name="Disk Path",
        default="",
        description="Resolved file path on disk, when available",
    )
    appearance: bpy.props.StringProperty(
        name="Appearance",
        default="",
        description="Entity appearance associated with this voice tag",
    )
    score: bpy.props.StringProperty(default="")


VOICE_FILTER_DEBOUNCE = 0.35
VOICE_POPULAR_LIMIT = 8
VOICE_PAGE_SIZE_DEFAULT = 300
VOICE_PAGE_SIZE_MIN = 50
VOICE_PAGE_SIZE_MAX = 2000
_voice_filter_last_change = 0.0
_voice_filter_scheduled = False
_voice_filter_pending_final = False  # True after timer fires — ensures one last run
_voice_speaker_counts = {}
_voice_popular_speakers_cache = []
_voice_page_syncing = False

def _get_display_text(scene, item):
    show_details = getattr(scene, "witcher_voice_show_details", True)
    if show_details and item.display_full:
        return item.display_full
    if not show_details and item.display_compact:
        return item.display_compact
    return item.name


# ---------------------------------------------------------------------------
# Robust search token parser
# ---------------------------------------------------------------------------
# Supports:
#   plain words        → AND substring match anywhere in blob
#   "quoted phrase"    → exact substring match (preserving word order)
#   -word              → exclude lines containing this term
#   id:NNN             → exact voice-ID prefix match
#   speaker:NAME / @NAME → filter to one speaker (sets speaker_filter)
#   word1|word2        → OR between alternatives for a single slot
# Returns a list of token dicts consumed by _matches_voice_filter_fast.

def _parse_search_tokens(raw_text):
    return browser_core.parse_search_tokens(raw_text)


def _matches_voice_filter_fast(blob, speaker, search_tokens, speaker_filter):
    return browser_core.matches_search_tokens(blob, speaker, search_tokens, speaker_filter)


# Keep old name for any external callers (falls back to fast version)
def _matches_voice_filter(node, search_terms, speaker_filter):
    """Legacy shim — converts old-style search_terms list to fast tokens."""
    speaker = node.get('speaker', '') if isinstance(node, dict) else getattr(node, 'speaker', '')
    blob = (node.get('search_blob') or node.get('name', '').lower()) if isinstance(node, dict) \
        else (node.search_blob or node.name.lower())
    legacy_tokens = [{'type': 'and', 'terms': [t]} for t in search_terms]
    return _matches_voice_filter_fast(blob, speaker, legacy_tokens, speaker_filter)

def _refresh_speaker_stats(nodes):
    global _voice_speaker_counts, _voice_popular_speakers_cache
    counts = {}
    for node in nodes:
        speaker = node.get('speaker', '') if isinstance(node, dict) else getattr(node, 'speaker', '')
        if speaker:
            counts[speaker] = counts.get(speaker, 0) + 1
    _voice_speaker_counts = counts
    _voice_popular_speakers_cache = [
        sp for sp, _ in sorted(counts.items(), key=lambda x: (-x[1], x[0]))
    ][:VOICE_POPULAR_LIMIT]

def _get_speaker_count(speaker):
    return _voice_speaker_counts.get(speaker, 0)


def _clamp_voice_page_size(value):
    return browser_core.clamp_page_size(
        value,
        VOICE_PAGE_SIZE_DEFAULT,
        VOICE_PAGE_SIZE_MIN,
        VOICE_PAGE_SIZE_MAX,
    )


def get_voice_filtered_count():
    return len(_voice_filtered_indices)


def get_voice_browser_stats(scene):
    ensure_voice_cache(bpy.context)
    total = len(_voice_node_cache)
    filtered = len(_voice_filtered_indices)
    page_size = _clamp_voice_page_size(getattr(scene, "witcher_voice_page_size", VOICE_PAGE_SIZE_DEFAULT))
    total_pages = browser_core.page_count(filtered, page_size)
    page_index = browser_core.clamp_page_index(getattr(scene, "witcher_voice_page_index", 0), total_pages)
    start = page_index * page_size
    end = min(start + page_size, filtered)
    return {
        "total": total,
        "filtered": filtered,
        "page_size": page_size,
        "page_index": page_index,
        "total_pages": total_pages,
        "visible_start": start + 1 if filtered else 0,
        "visible_end": end,
    }


def _sync_voice_page_number(scene, total_pages=None):
    global _voice_page_syncing
    if scene is None or not hasattr(scene, "witcher_voice_page_number"):
        return
    if total_pages is None:
        total_pages = get_voice_browser_stats(scene)["total_pages"]
    page_number = browser_core.page_number_from_index(
        getattr(scene, "witcher_voice_page_index", 0),
        total_pages,
    )
    if int(getattr(scene, "witcher_voice_page_number", 1) or 1) == page_number:
        return
    _voice_page_syncing = True
    try:
        scene.witcher_voice_page_number = page_number
    finally:
        _voice_page_syncing = False


def _get_selected_voice_id(scene):
    if 0 <= scene.witcher_voice_list_index < len(scene.witcher_voice_list):
        return scene.witcher_voice_list[scene.witcher_voice_list_index].voiceLineId
    return str(getattr(scene, "witcher_voice_selected_id", "") or "")


def _set_selected_voice_id(scene, voice_id):
    if hasattr(scene, "witcher_voice_selected_id"):
        scene.witcher_voice_selected_id = voice_id or ""


def _set_voice_filter_anchor(scene, voice_id=None):
    if scene is None or not hasattr(scene, "witcher_voice_filter_anchor_id"):
        return ""
    voice_id = str(voice_id if voice_id is not None else _get_selected_voice_id(scene) or "").strip()
    if voice_id:
        scene.witcher_voice_filter_anchor_id = voice_id
    return voice_id


def _clear_voice_filter_anchor(scene, voice_id=""):
    if scene is None or not hasattr(scene, "witcher_voice_filter_anchor_id"):
        return
    if not voice_id or str(getattr(scene, "witcher_voice_filter_anchor_id", "") or "") == str(voice_id or ""):
        scene.witcher_voice_filter_anchor_id = ""


def _remember_previous_voice_selection(scene, new_voice_id):
    if scene is None or not hasattr(scene, "witcher_voice_previous_selected_id"):
        return
    old_voice_id = str(getattr(scene, "witcher_voice_selected_id", "") or "").strip()
    new_voice_id = str(new_voice_id or "").strip()
    if old_voice_id and new_voice_id and old_voice_id != new_voice_id:
        scene.witcher_voice_previous_selected_id = old_voice_id


def _filtered_voice_position(voice_id):
    voice_id = str(voice_id or "").strip()
    if not voice_id:
        return -1
    for filtered_pos, cache_idx in enumerate(_voice_filtered_indices):
        if cache_idx < 0 or cache_idx >= len(_voice_node_cache):
            continue
        if str(_voice_node_cache[cache_idx].get("voiceLineId", "") or "") == voice_id:
            return filtered_pos
    return -1


def _set_list_item_from_node(item, node):
    item.game = node.get('game', VOICE_GAME_W3)
    item.name = node.get('display_full') or node.get('name', '')
    item.nodeIndex = node.get('selfIndex', -1)
    item.childCount = node.get('childCount', 0)
    item.voiceLineId = node.get('voiceLineId', '')
    item.speaker = node.get('speaker', '')
    item.line_id = node.get('line_id', '')
    item.duration = node.get('duration', '')
    item.text = node.get('text', '')
    item.display_full = node.get('display_full', '')
    item.display_compact = node.get('display_compact', '')
    item.source_path = node.get('source_path', '')
    item.speaker_candidates = "; ".join(node.get('speaker_candidates') or [])
    item.scene_path = node.get('scene_path', '')
    item.entity_path = node.get('entity_path', '')


def _normalize_voice_scene_path(value):
    return browser_core.normalize_repo_path(value)


def _node_scene_paths(node):
    return browser_core.item_scene_paths(node)


def _node_matches_scene_filter(node, scene_filter):
    return browser_core.item_matches_scene_filter(node, scene_filter)


def _is_dialogue_preview_playing(game, line_id):
    try:
        from ..strings_browser import ui_strings_browser

        return ui_strings_browser._is_voice_preview_playing(game, line_id)
    except Exception:
        return False


def _node_display_text(scene, node):
    show_details = getattr(scene, "witcher_voice_show_details", True)
    if show_details:
        return node.get('display_full') or node.get('name', '')
    return node.get('display_compact') or node.get('name', '')


def _refresh_voice_page(scene, selected_id=None, *, jump_to_selected=False):
    if not hasattr(scene, "witcher_voice_list"):
        return False

    page_size = _clamp_voice_page_size(getattr(scene, "witcher_voice_page_size", VOICE_PAGE_SIZE_DEFAULT))
    if getattr(scene, "witcher_voice_page_size", page_size) != page_size:
        scene.witcher_voice_page_size = page_size

    filtered_total = len(_voice_filtered_indices)
    total_pages = browser_core.page_count(filtered_total, page_size)
    page_index = browser_core.clamp_page_index(getattr(scene, "witcher_voice_page_index", 0), total_pages)
    if jump_to_selected and selected_id:
        selected_pos = _filtered_voice_position(selected_id)
        if selected_pos >= 0:
            page_index = selected_pos // page_size
    page_index = browser_core.clamp_page_index(page_index, total_pages)
    scene.witcher_voice_page_index = page_index
    _sync_voice_page_number(scene, total_pages)

    page_start, page_end, _page_index, _total_pages = browser_core.page_bounds(
        filtered_total,
        page_index,
        page_size,
    )
    page_indices = _voice_filtered_indices[page_start:page_end]

    display_list = scene.witcher_voice_list
    display_list.clear()

    selected_visible_idx = -1
    for local_idx, cache_idx in enumerate(page_indices):
        if cache_idx < 0 or cache_idx >= len(_voice_node_cache):
            continue
        node = _voice_node_cache[cache_idx]
        item = display_list.add()
        _set_list_item_from_node(item, node)
        if selected_id and item.voiceLineId == selected_id:
            selected_visible_idx = local_idx

    if selected_visible_idx >= 0:
        scene.witcher_voice_list_index = selected_visible_idx
    elif len(display_list):
        scene.witcher_voice_list_index = 0
    else:
        scene.witcher_voice_list_index = -1

    _set_selected_voice_id(scene, _get_selected_voice_id(scene))
    refresh_selected_voice_associated_paths(bpy.context, force=True)
    return selected_visible_idx >= 0

def _parse_search_text(text):
    return browser_core.parse_search_text(text)

def _strip_speaker_tags(text):
    clean_text, _ = _parse_search_text(text)
    return clean_text

def _get_selected_speaker(context):
    scene = context.scene
    if 0 <= scene.witcher_voice_list_index < len(scene.witcher_voice_list):
        return scene.witcher_voice_list[scene.witcher_voice_list_index].speaker
    return ""

def _get_selected_voice_item(context):
    scene = getattr(context, "scene", None)
    if scene is None or not hasattr(scene, "witcher_voice_list"):
        return None
    index = getattr(scene, "witcher_voice_list_index", -1)
    if 0 <= index < len(scene.witcher_voice_list):
        return scene.witcher_voice_list[index]
    return None


def _resolve_voice_repo_path(context, repo_path: str, game: str) -> str:
    repo_path = str(repo_path or "").strip()
    if not repo_path:
        return ""
    if os.path.isabs(repo_path) and os.path.exists(repo_path):
        return os.path.normpath(repo_path)

    game = str(game or VOICE_GAME_W3).upper()
    if game == VOICE_GAME_W2:
        try:
            from ..CR2W.witcher_cache.SceneDialog.w2_scene_dialog import resolve_w2_repo_path

            return resolve_w2_repo_path(repo_path) or ""
        except Exception:
            log.debug("Failed to resolve W2 repo path: %s", repo_path, exc_info=True)
            return ""

    try:
        from ..CR2W.witcher_cache.SceneDialog.w3_scene_dialog import resolve_w3_repo_path

        return resolve_w3_repo_path(repo_path) or ""
    except Exception:
        log.debug("Failed to resolve W3 repo path: %s", repo_path, exc_info=True)
        return ""


def _voice_extracted_root(context, game: str, *, create_root=False) -> str:
    game = str(game or VOICE_GAME_W3).upper()
    try:
        root = get_w2_unbundle_path(context) if game == VOICE_GAME_W2 else get_uncook_path(context)
    except Exception:
        try:
            prefs = get_all_addon_prefs(context)
        except Exception:
            prefs = None
        attr = "w2_unbundle_path" if game == VOICE_GAME_W2 else "uncook_path"
        root = str(getattr(prefs, attr, "") or "") if prefs else ""
    root = os.path.normpath(bpy.path.abspath(str(root or "").strip())) if root else ""
    if not root:
        return ""
    if create_root:
        os.makedirs(root, exist_ok=True)
    return root


def _voice_extracted_target_path(context, repo_path: str, game: str, *, create_root=False) -> str:
    repo_path = str(repo_path or "").strip().replace("/", "\\").lstrip("\\")
    if not repo_path or os.path.isabs(repo_path):
        return ""

    root = _voice_extracted_root(context, game, create_root=create_root)
    if not root:
        return ""
    return os.path.normpath(os.path.join(root, repo_path))


def _path_is_under(path: str, root: str) -> bool:
    if not path or not root:
        return False
    try:
        path_norm = os.path.normcase(os.path.normpath(path))
        root_norm = os.path.normcase(os.path.normpath(root))
        return os.path.commonpath([path_norm, root_norm]) == root_norm
    except Exception:
        return False


def _resolve_voice_extracted_repo_path(context, repo_path: str, game: str) -> str:
    if os.path.isabs(str(repo_path or "")):
        root = _voice_extracted_root(context, game)
        repo_abs = os.path.normpath(repo_path)
        return repo_abs if root and _path_is_under(repo_abs, root) and os.path.exists(repo_abs) else ""
    candidate = _voice_extracted_target_path(context, repo_path, game)
    if not candidate:
        return ""
    try:
        from ..CR2W.common_blender import win_path_exists

        exists = win_path_exists(candidate)
    except Exception:
        exists = os.path.exists(candidate)
    return candidate if exists else ""


def _voice_repo_path_variants(repo_path: str, game: str):
    repo_path = str(repo_path or "").strip().replace("/", "\\").lstrip("\\")
    if not repo_path:
        return []
    variants = []

    def add(path):
        path = str(path or "").strip().replace("/", "\\").lstrip("\\")
        if path and path.lower() not in {item.lower() for item in variants}:
            variants.append(path)

    add(repo_path)
    lower = repo_path.lower()
    if lower.startswith("data\\"):
        add(repo_path[5:])
    if lower.startswith("r4data\\"):
        add(repo_path[7:])
    if str(game or "").upper() == VOICE_GAME_W2:
        if lower.startswith("dataoff\\"):
            add(repo_path[8:])
        if lower.startswith("data\\"):
            add(repo_path[5:])
        if not lower.startswith("game\\") and not lower.startswith("templates\\"):
            add("game\\" + repo_path)
    else:
        if lower.startswith("quests\\skellige\\quest_files\\"):
            add("quests\\sidequests\\skellige\\quest_files\\" + repo_path[len("quests\\skellige\\quest_files\\"):])
    return variants


def _copy_voice_repo_source_to_extracted(context, repo_path: str, game: str) -> str:
    target_path = _voice_extracted_target_path(context, repo_path, game, create_root=True)
    if not target_path:
        return ""
    if os.path.exists(target_path):
        return os.path.normpath(target_path)

    for variant in _voice_repo_path_variants(repo_path, game):
        source_path = _resolve_voice_repo_path(context, variant, game)
        if not source_path or not os.path.isfile(source_path):
            continue
        if os.path.normcase(os.path.normpath(source_path)) == os.path.normcase(os.path.normpath(target_path)):
            return os.path.normpath(source_path)
        try:
            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            shutil.copy2(source_path, target_path)
            return os.path.normpath(target_path)
        except Exception:
            log.debug("Failed to copy associated repo source %s to %s", source_path, target_path, exc_info=True)
            return ""
    return ""


def _extract_voice_repo_path(context, repo_path: str, game: str) -> str:
    repo_path = str(repo_path or "").strip()
    if not repo_path or os.path.isabs(repo_path):
        return ""

    game = str(game or VOICE_GAME_W3).upper()
    expected_root = _voice_extracted_root(context, game, create_root=True)
    expected_path = _voice_extracted_target_path(context, repo_path, game, create_root=True)
    try:
        from . import ui_file_browser

        extracted = ""
        for variant in _voice_repo_path_variants(repo_path, game):
            if game == VOICE_GAME_W2:
                extracted = ui_file_browser.ensure_witcher2_bundle_item_extracted(
                    context,
                    variant,
                    overwrite=False,
                )
            else:
                from ..CR2W.common_blender import vanilla_only_repo_context

                with vanilla_only_repo_context():
                    extracted = ui_file_browser.ensure_bundle_item_extracted(
                        context,
                        variant,
                        loadmods=False,
                    )
            if extracted and os.path.exists(extracted):
                break
        if expected_path and os.path.exists(expected_path):
            return os.path.normpath(expected_path)
        if extracted and os.path.exists(extracted):
            extracted = os.path.normpath(extracted)
            if not expected_root or _path_is_under(extracted, expected_root):
                return extracted
    except Exception:
        log.debug("Failed to extract %s repo path from bundles: %s", game, repo_path, exc_info=True)
    return _copy_voice_repo_source_to_extracted(context, repo_path, game)


def _resolve_or_extract_voice_repo_path(context, repo_path: str, game: str) -> str:
    resolved = _resolve_voice_repo_path(context, repo_path, game)
    if resolved:
        return resolved
    return _extract_voice_repo_path(context, repo_path, game)


def _resolve_or_extract_voice_extracted_repo_path(context, repo_path: str, game: str) -> str:
    resolved = _resolve_voice_extracted_repo_path(context, repo_path, game)
    if resolved:
        return resolved
    return _extract_voice_repo_path(context, repo_path, game)


def _set_associated_path_resolved(context, repo_path: str, game: str, resolved_path: str) -> None:
    scene = getattr(context, "scene", None)
    collection = getattr(scene, "witcher_voice_associated_paths", None) if scene is not None else None
    if collection is None:
        return

    repo_key = str(repo_path or "").replace("/", "\\").lower()
    game_key = str(game or "").upper()
    for item in collection:
        item_repo_key = str(getattr(item, "repo_path", "") or "").replace("/", "\\").lower()
        item_game_key = str(getattr(item, "game", "") or "").upper()
        if item_repo_key == repo_key and item_game_key == game_key:
            item.resolved_path = resolved_path or ""


def _selected_voice_metadata(item):
    if item is None:
        return None, {}
    game = str(getattr(item, "game", VOICE_GAME_W3) or VOICE_GAME_W3).upper()
    try:
        if game == VOICE_GAME_W2:
            from ..CR2W.witcher_cache.SceneDialog.w2_scene_dialog import LoadWitcher2SceneDialogMetadata

            metadata = LoadWitcher2SceneDialogMetadata()
        else:
            from ..CR2W.witcher_cache.SceneDialog.w3_scene_dialog import LoadWitcher3SceneDialogMetadata

            metadata = LoadWitcher3SceneDialogMetadata()
        line_info = metadata.get_line(getattr(item, "voiceLineId", "")) if metadata else {}
        return metadata, line_info or {}
    except Exception:
        log.debug("Failed to load selected dialogue metadata.", exc_info=True)
        return None, {}


def _add_unique_associated_path(entries, seen, *, kind, game, label, repo_path, appearance="", score="", source=""):
    repo_path = str(repo_path or "").strip().replace("/", "\\")
    if not repo_path:
        return
    appearance = str(appearance or "").strip()
    key = (kind, repo_path.lower(), appearance.lower())
    if key in seen:
        return
    seen.add(key)
    entries.append({
        "kind": kind,
        "game": game,
        "label": label,
        "source": str(source or ""),
        "repo_path": repo_path,
        "appearance": appearance,
        "score": str(score or ""),
    })


def _append_entity_path(entities, seen, value):
    if isinstance(value, dict):
        repo_path = str(value.get("path", "") or "").strip()
        appearance = str(value.get("appearance", "") or "").strip()
        source = str(value.get("source", "") or "").strip()
    else:
        repo_path = str(value or "").strip()
        appearance = ""
        source = ""
    repo_path = repo_path.replace("/", "\\")
    if not repo_path:
        return
    key = (repo_path.lower(), appearance.lower())
    if key in seen:
        return
    seen.add(key)
    item = {"path": repo_path}
    if appearance:
        item["appearance"] = appearance
    if source:
        item["source"] = source
    entities.append(item)


def _entity_dict_path(entity):
    return str(entity.get("path", "") if isinstance(entity, dict) else entity or "").strip().replace("/", "\\")


def _entity_dict_appearance(entity):
    return str(entity.get("appearance", "") if isinstance(entity, dict) else "").strip()


def _voicetag_entity_paths(line_info):
    rows = []
    seen = set()
    for entity in (line_info.get("voice_tag_entity_paths", []) or []):
        path = _entity_dict_path(entity)
        appearance = _entity_dict_appearance(entity)
        if not path:
            continue
        key = (path.lower(), appearance.lower())
        if key in seen:
            continue
        seen.add(key)
        rows.append({"path": path, "appearance": appearance, "source": "entity_voice_tag"})
    for entity in (line_info.get("entity_paths", []) or []):
        if not isinstance(entity, dict):
            continue
        source = str(entity.get("source", "") or "")
        appearance = _entity_dict_appearance(entity)
        if source and source != "entity_voice_tag":
            continue
        if source != "entity_voice_tag" and not appearance:
            continue
        path = _entity_dict_path(entity)
        if not path:
            continue
        key = (path.lower(), appearance.lower())
        if key in seen:
            continue
        seen.add(key)
        rows.append({"path": path, "appearance": appearance, "source": "entity_voice_tag"})
    return rows


def _scene_actor_entities_for_line(line_info, item):
    entity_path = str(line_info.get("entity_path", "") or getattr(item, "entity_path", "") or "").strip().replace("/", "\\")
    rows = []
    seen = set()
    for entity in (line_info.get("entity_paths", []) or []):
        if not isinstance(entity, dict):
            continue
        if str(entity.get("source", "") or "") != "scene_actor":
            continue
        path = _entity_dict_path(entity)
        if not path:
            continue
        appearance = _entity_dict_appearance(entity)
        key = (path.lower(), appearance.lower())
        if key in seen:
            continue
        seen.add(key)
        row = {"path": path, "source": "scene_actor"}
        if appearance:
            row["appearance"] = appearance
        rows.append(row)
    if rows:
        return rows
    if not entity_path:
        return []
    matches = [
        entity for entity in _voicetag_entity_paths(line_info)
        if _entity_dict_path(entity).lower() == entity_path.lower() and _entity_dict_appearance(entity)
    ]
    result = {"path": entity_path, "source": "scene_actor"}
    if len(matches) == 1:
        result["appearance"] = _entity_dict_appearance(matches[0])
    return [result]


def _scene_actor_entity_for_line(line_info, item):
    rows = _scene_actor_entities_for_line(line_info, item)
    return rows[0] if rows else {}


def get_selected_voice_voicetag_entity_count(context):
    item = _get_selected_voice_item(context)
    if item is None:
        return 0
    _metadata, line_info = _selected_voice_metadata(item)
    scene_keys = {
        (
            _entity_dict_path(scene_actor).lower(),
            _entity_dict_appearance(scene_actor).lower(),
        )
        for scene_actor in _scene_actor_entities_for_line(line_info, item)
        if scene_actor
    }
    count = 0
    for entity in _voicetag_entity_paths(line_info):
        key = (_entity_dict_path(entity).lower(), _entity_dict_appearance(entity).lower())
        if key in scene_keys:
            continue
        count += 1
    return count


def get_selected_voice_associated_paths(context, *, max_scenes=5, max_entities=64, include_voicetag_entities=False):
    item = _get_selected_voice_item(context)
    if item is None:
        return []
    game = str(getattr(item, "game", VOICE_GAME_W3) or VOICE_GAME_W3).upper()
    metadata, line_info = _selected_voice_metadata(item)
    entries = []
    seen = set()

    source_scenes = list(line_info.get("source_scenes", []) or [])
    scene_path = str(line_info.get("scene_path", "") or getattr(item, "scene_path", "") or "").strip()
    if scene_path and scene_path not in source_scenes:
        source_scenes.insert(0, scene_path)
    for idx, scene_repo_path in enumerate(source_scenes[:max_scenes]):
        _add_unique_associated_path(
            entries,
            seen,
            kind="scene",
            game=game,
            label=".w2scene" if idx == 0 else f".w2scene {idx + 1}",
            repo_path=scene_repo_path,
        )

    entity_paths = []
    entity_seen = set()
    for scene_actor in _scene_actor_entities_for_line(line_info, item):
        _append_entity_path(entity_paths, entity_seen, scene_actor)
    if include_voicetag_entities:
        for entity in _voicetag_entity_paths(line_info):
            _append_entity_path(entity_paths, entity_seen, entity)

    for idx, entity in enumerate(entity_paths[:max_entities]):
        if not isinstance(entity, dict):
            continue
        repo_path = str(entity.get("path", "") or "").strip()
        if not repo_path:
            continue
        _add_unique_associated_path(
            entries,
            seen,
            kind="entity",
            game=game,
            label=f"Template {idx + 1}",
            repo_path=repo_path,
            appearance=str(entity.get("appearance", "") or ""),
            source=str(entity.get("source", "") or ""),
        )
    return entries


def _selected_voice_associated_paths_key(context):
    scene = getattr(context, "scene", None)
    item = _get_selected_voice_item(context)
    if item is None:
        return ""
    return "|".join((
        str(getattr(item, "game", "") or ""),
        str(getattr(item, "voiceLineId", "") or ""),
        str(getattr(item, "speaker", "") or ""),
        str(getattr(item, "scene_path", "") or ""),
        str(getattr(item, "entity_path", "") or ""),
        "all" if bool(getattr(scene, "witcher_voice_show_all_voicetag_entities", False)) else "scene",
    ))


def refresh_selected_voice_associated_paths(context, *, force=False):
    scene = getattr(context, "scene", None)
    if scene is None or not hasattr(scene, "witcher_voice_associated_paths"):
        return
    key = _selected_voice_associated_paths_key(context)
    if not force and getattr(scene, "witcher_voice_associated_paths_key", "") == key:
        return

    collection = scene.witcher_voice_associated_paths
    collection.clear()
    if not key:
        scene.witcher_voice_associated_paths_key = ""
        return

    include_all = bool(getattr(scene, "witcher_voice_show_all_voicetag_entities", False))
    for entry in get_selected_voice_associated_paths(context, include_voicetag_entities=include_all):
        item = collection.add()
        item.kind = entry.get("kind", "")
        item.game = entry.get("game", VOICE_GAME_W3)
        item.label = entry.get("label", "")
        item.source = entry.get("source", "")
        item.repo_path = entry.get("repo_path", "")
        item.appearance = entry.get("appearance", "")
        item.score = entry.get("score", "")
        item.resolved_path = _resolve_voice_extracted_repo_path(context, item.repo_path, item.game)
    scene.witcher_voice_associated_paths_key = key


def _selected_voice_entity_candidate(context):
    item = _get_selected_voice_item(context)
    if item is None:
        return {}
    game = str(getattr(item, "game", VOICE_GAME_W3) or VOICE_GAME_W3).upper()
    if game not in {VOICE_GAME_W2, VOICE_GAME_W3}:
        return {}
    entity_path = str(getattr(item, "entity_path", "") or "").strip()
    if entity_path:
        return {"path": entity_path, "source": "primary"}

    _metadata, line_info = _selected_voice_metadata(item)
    for entity in _scene_actor_entities_for_line(line_info, item):
        path = _entity_dict_path(entity)
        if path:
            return dict(entity, path=path)
    for entity in _voicetag_entity_paths(line_info):
        path = _entity_dict_path(entity)
        if path:
            return dict(entity, path=path)
    return {}


def get_selected_voice_entity_path(context):
    return _entity_dict_path(_selected_voice_entity_candidate(context))

def _resolve_voice_entity_path(context, entity_path: str, game: str) -> str:
    entity_path = str(entity_path or "").strip()
    if not entity_path:
        return ""

    return _resolve_or_extract_voice_extracted_repo_path(context, entity_path, game)

def _resolve_w2_voice_entity_path(context, entity_path: str) -> str:
    return _resolve_voice_entity_path(context, entity_path, VOICE_GAME_W2)


def _import_voice_entity(context, abs_file_path: str, selected_appearance_name=""):
    from ..importers import import_entity

    selected_appearance_name = str(selected_appearance_name or "").strip()
    metadata = import_entity.get_entity_appearance_metadata(abs_file_path)
    w2ent_mode = import_entity.classify_entity_import_metadata(metadata, context=context)
    if w2ent_mode == "character":
        default_appearance_name = selected_appearance_name or str(metadata.get("default_name", "") or "").strip()
        return import_entity.import_direct_entity_file(
            abs_file_path,
            False,
            0 if default_appearance_name else 1,
            None,
            selected_appearance_name=default_appearance_name,
        )
    if not import_entity.try_apply_inventory_file_to_selected_character(context, abs_file_path):
        return import_entity.import_direct_entity_file(abs_file_path, False, 0, None)
    return None


def _import_w2_voice_entity(context, abs_file_path: str):
    return _import_voice_entity(context, abs_file_path)

def _get_sequence_editor_strips(sequence_editor):
    if sequence_editor is None:
        return None
    strips = getattr(sequence_editor, "sequences", None)
    if strips is None:
        strips = getattr(sequence_editor, "strips", None)
    return strips

def _get_next_sound_channel(scene):
    strips = _get_sequence_editor_strips(getattr(scene, "sequence_editor", None))
    if strips is None:
        return 1
    channels = [
        strip.channel for strip in strips
        if strip.type == 'SOUND'
    ]
    return max(channels) + 1 if channels else 1


_DIALOG_AUDIO_STRIP_PROPS = (
    dialog_language.DIALOG_SUBTITLE_TEXT_PROP,
    dialog_language.DIALOG_SUBTITLE_LINE_ID_PROP,
    dialog_language.DIALOG_SUBTITLE_SPEAKER_PROP,
    dialog_language.DIALOG_SUBTITLE_SOURCE_PROP,
    dialog_language.DIALOG_SUBTITLE_SOURCE_PATH_PROP,
    dialog_language.DIALOG_SUBTITLE_LANGUAGE_PROP,
    dialog_language.DIALOG_AUDIO_LANGUAGE_PROP,
    "witcher_cutscene_dialog_audio",
    "witcher_cutscene_dialog_line_id",
    "witcher_cutscene_dialog_text",
    "witcher_cutscene_dialog_sound_event",
    "witcher_cutscene_dialog_item_path",
    "witcher_cutscene_dialog_source_path",
    "witcher_w2scene_dialog_text",
    "witcher_w2scene_dialog_line_id",
    "witcher_w2scene_section_audio",
    "witcher_w2scene_source",
    "witcher_w2scene_section",
    "witcher_w2_voice_file",
)


def _strip_has_custom_prop(strip, prop_name):
    try:
        return prop_name in strip.keys()
    except Exception:
        try:
            return strip.get(prop_name, None) is not None
        except Exception:
            return False


def _is_dialog_audio_strip(strip):
    if getattr(strip, "type", None) != 'SOUND':
        return False
    return any(_strip_has_custom_prop(strip, prop_name) for prop_name in _DIALOG_AUDIO_STRIP_PROPS)


def clear_dialog_audio_and_subtitles(scene):
    strips = _get_sequence_editor_strips(getattr(scene, "sequence_editor", None))
    if strips is None:
        return 0

    to_remove = [strip for strip in list(strips) if _is_dialog_audio_strip(strip)]
    removed = 0
    for strip in to_remove:
        try:
            strips.remove(strip)
            removed += 1
        except Exception:
            log.debug("Could not remove dialogue audio strip %s", getattr(strip, "name", ""), exc_info=True)
    return removed


def _is_pinned(scene, speaker):
    if not speaker:
        return False
    for pin in scene.witcher_voice_pinned_speakers:
        if pin.name == speaker:
            return True
    return False

def _set_speaker_filter(scene, context, speaker):
    _set_voice_filter_anchor(scene)
    scene.witcher_voice_speaker_filter = speaker.upper() if speaker else ""
    if hasattr(scene, "witcher_voice_page_index"):
        scene.witcher_voice_page_index = 0
    if scene.witcher_voice_search_text:
        scene.witcher_voice_search_text = _strip_speaker_tags(scene.witcher_voice_search_text)
    _apply_voice_filter(context)


def _set_scene_filter(scene, context, scene_path):
    _set_voice_filter_anchor(scene)
    if hasattr(scene, "witcher_voice_scene_filter"):
        scene.witcher_voice_scene_filter = str(scene_path or "").strip().replace("/", "\\")
    if hasattr(scene, "witcher_voice_page_index"):
        scene.witcher_voice_page_index = 0
    _apply_voice_filter(context)


def _get_effective_speaker(scene):
    _, speaker_from_search = _parse_search_text(scene.witcher_voice_search_text.strip())
    if speaker_from_search:
        return speaker_from_search
    return scene.witcher_voice_speaker_filter.strip().upper()


def _get_effective_scene_filter(scene):
    return str(getattr(scene, "witcher_voice_scene_filter", "") or "").strip().replace("/", "\\")


def _apply_voice_filter(context):
    global _voice_filtered_indices, _voice_filter_pending_final
    scene = context.scene
    if not hasattr(scene, "witcher_voice_list"):
        return

    ensure_voice_cache(context)
    if not _voice_node_cache:
        _voice_filtered_indices = []
        if hasattr(scene, "witcher_voice_list"):
            scene.witcher_voice_list.clear()
        return

    raw_search_text = scene.witcher_voice_search_text.strip()
    search_tokens, speaker_from_search = _parse_search_tokens(raw_search_text)
    speaker_filter = speaker_from_search or scene.witcher_voice_speaker_filter.strip().upper()
    scene_filter = _get_effective_scene_filter(scene)

    selected_id = str(getattr(scene, "witcher_voice_filter_anchor_id", "") or "").strip() or _get_selected_voice_id(scene)

    # Fast path: no filters at all → all indices
    if not search_tokens and not speaker_filter and not scene_filter:
        _voice_filtered_indices = list(range(len(_voice_node_cache)))
        _voice_filter_pending_final = False
        if _refresh_voice_page(scene, selected_id=selected_id, jump_to_selected=True):
            _clear_voice_filter_anchor(scene, selected_id)
        return

    # Build filtered list with the tight inner loop
    result = []
    cache = _voice_node_cache
    n = len(cache)
    for idx in range(n):
        node = cache[idx]
        if scene_filter and not _node_matches_scene_filter(node, scene_filter):
            continue
        blob = node.get('search_blob', '') or ''
        speaker = node.get('speaker', '') or ''
        if _matches_voice_filter_fast(blob, speaker, search_tokens, speaker_filter):
            result.append(idx)

    _voice_filtered_indices = result
    _voice_filter_pending_final = False
    if _refresh_voice_page(scene, selected_id=selected_id, jump_to_selected=True):
        _clear_voice_filter_anchor(scene, selected_id)

def _voice_filter_timer():
    global _voice_filter_scheduled, _voice_filter_pending_final
    elapsed = time.time() - _voice_filter_last_change
    if elapsed < VOICE_FILTER_DEBOUNCE:
        # Still debouncing — come back soon
        _voice_filter_pending_final = True
        return VOICE_FILTER_DEBOUNCE - elapsed + 0.02
    # Debounce window passed
    _voice_filter_scheduled = False
    if bpy.context and bpy.context.scene:
        _apply_voice_filter(bpy.context)
    return None


def _schedule_voice_filter():
    global _voice_filter_last_change, _voice_filter_scheduled
    _voice_filter_last_change = time.time()
    if not _voice_filter_scheduled:
        _voice_filter_scheduled = True
        bpy.app.timers.register(_voice_filter_timer, first_interval=VOICE_FILTER_DEBOUNCE + 0.02)


def _on_voice_search_update(self, context):
    if context is not None and getattr(context, "scene", None) is not None:
        _set_voice_filter_anchor(context.scene)
        if hasattr(context.scene, "witcher_voice_page_index"):
            context.scene.witcher_voice_page_index = 0
    _schedule_voice_filter()


def _on_voice_scene_filter_update(self, context):
    if context is not None and getattr(context, "scene", None) is not None:
        _set_voice_filter_anchor(context.scene)
        if hasattr(context.scene, "witcher_voice_page_index"):
            context.scene.witcher_voice_page_index = 0
    _schedule_voice_filter()


def _on_voice_page_size_update(self, context):
    if context is None or getattr(context, "scene", None) is None:
        return
    scene = context.scene
    clamped = _clamp_voice_page_size(getattr(scene, "witcher_voice_page_size", VOICE_PAGE_SIZE_DEFAULT))
    if scene.witcher_voice_page_size != clamped:
        scene.witcher_voice_page_size = clamped
        return
    if _voice_filtered_indices:
        _refresh_voice_page(scene, selected_id=_get_selected_voice_id(scene), jump_to_selected=True)
    elif _voice_node_cache:
        _apply_voice_filter(context)


def _on_voice_page_number_update(self, context):
    global _voice_page_syncing
    if _voice_page_syncing or context is None or getattr(context, "scene", None) is None:
        return
    scene = context.scene
    if not _voice_filtered_indices and _voice_node_cache:
        _apply_voice_filter(context)
    stats = get_voice_browser_stats(scene)
    target_index = browser_core.page_index_from_number(
        getattr(scene, "witcher_voice_page_number", 1),
        stats["total_pages"],
    )
    page_number = target_index + 1
    if scene.witcher_voice_page_number != page_number:
        _voice_page_syncing = True
        try:
            scene.witcher_voice_page_number = page_number
        finally:
            _voice_page_syncing = False
    if scene.witcher_voice_page_index != target_index:
        scene.witcher_voice_page_index = target_index
        _refresh_voice_page(scene, selected_id=_get_selected_voice_id(scene))


def _on_voice_game_update(self, context):
    global _voice_node_cache, _voice_cache_loaded, _voice_filtered_indices, _voice_cache_identity_loaded
    scene = getattr(context, "scene", None) if context is not None else None
    _voice_node_cache = []
    _voice_filtered_indices = []
    _voice_cache_loaded = False
    _voice_cache_identity_loaded = None
    if scene is not None:
        if hasattr(scene, "witcher_voice_list"):
            scene.witcher_voice_list.clear()
        if hasattr(scene, "witcher_voice_list_index"):
            scene.witcher_voice_list_index = -1
        if hasattr(scene, "witcher_voice_selected_id"):
            scene.witcher_voice_selected_id = ""
        if hasattr(scene, "witcher_voice_previous_selected_id"):
            scene.witcher_voice_previous_selected_id = ""
        if hasattr(scene, "witcher_voice_filter_anchor_id"):
            scene.witcher_voice_filter_anchor_id = ""
        if hasattr(scene, "witcher_voice_page_index"):
            scene.witcher_voice_page_index = 0
        if hasattr(scene, "witcher_voice_speaker_filter"):
            scene.witcher_voice_speaker_filter = ""
        if hasattr(scene, "witcher_voice_scene_filter"):
            scene.witcher_voice_scene_filter = ""
        if hasattr(scene, "witcher_voice_show_all_voicetag_entities"):
            scene.witcher_voice_show_all_voicetag_entities = False
        if hasattr(scene, "witcher_voice_associated_paths"):
            scene.witcher_voice_associated_paths.clear()
        if hasattr(scene, "witcher_voice_associated_paths_key"):
            scene.witcher_voice_associated_paths_key = ""
    _schedule_deferred_voice_filter()


def _voice_dialog_strip_props(line_id, text="", speaker="", source="voice_browser", context=None, source_path=""):
    line_id = str(line_id or "").strip()
    text_language = dialog_language.get_active_text_language(context)
    text = str(text or "").strip()
    if not text and line_id and source != "voice_browser_w2":
        text = dialog_language.resolve_localized_text(line_id, language=text_language)
    props = {
        dialog_language.DIALOG_SUBTITLE_TEXT_PROP: text,
        dialog_language.DIALOG_SUBTITLE_LINE_ID_PROP: line_id,
        dialog_language.DIALOG_SUBTITLE_SPEAKER_PROP: str(speaker or ""),
        dialog_language.DIALOG_SUBTITLE_SOURCE_PROP: source,
        dialog_language.DIALOG_SUBTITLE_LANGUAGE_PROP: text_language,
    }
    source_path = str(source_path or "").strip()
    if source_path:
        props[dialog_language.DIALOG_SUBTITLE_SOURCE_PATH_PROP] = source_path
    return props


def _merge_voice_dialog_strip_props(line_id, strip_props=None, context=None):
    props = dict(strip_props or {})
    text_language = dialog_language.get_active_text_language(context)
    text = str(
        props.get(dialog_language.DIALOG_SUBTITLE_TEXT_PROP, "")
        or props.get("witcher_cutscene_dialog_text", "")
        or props.get("witcher_w2scene_dialog_text", "")
        or ""
    ).strip()
    subtitle_line_id = str(
        props.get(dialog_language.DIALOG_SUBTITLE_LINE_ID_PROP, "")
        or props.get("witcher_cutscene_dialog_line_id", "")
        or line_id
        or ""
    ).strip()
    defaults = _voice_dialog_strip_props(
        subtitle_line_id,
        text=text,
        speaker=props.get(dialog_language.DIALOG_SUBTITLE_SPEAKER_PROP, ""),
        source=props.get(dialog_language.DIALOG_SUBTITLE_SOURCE_PROP, "voice"),
        context=context,
    )
    defaults[dialog_language.DIALOG_SUBTITLE_LANGUAGE_PROP] = text_language
    defaults.update(props)
    return defaults


def _on_voice_list_index_update(self, context):
    if context is None or getattr(context, "scene", None) is None:
        return
    scene = context.scene
    selected_id = _get_selected_voice_id(scene)
    _remember_previous_voice_selection(scene, selected_id)
    _set_selected_voice_id(scene, selected_id)
    if hasattr(scene, "witcher_voice_show_all_voicetag_entities"):
        scene.witcher_voice_show_all_voicetag_entities = False
    refresh_selected_voice_associated_paths(context, force=True)
    if not getattr(scene, "witcher_voice_load_on_select", False):
        return
    # Load on select: fire immediately if a valid item is highlighted
    idx = scene.witcher_voice_list_index
    if idx < 0 or idx >= len(scene.witcher_voice_list):
        return
    item = scene.witcher_voice_list[idx]
    if not item.voiceLineId:
        return
    active_arm = _get_active_armature(context)
    if active_arm and not _armature_has_face_morphs(active_arm):
        _auto_load_face_morphs(context, active_arm)
    _at_frame = context.scene.frame_current if getattr(scene, 'witcher_anim_nla_mode', 'REPLACE') == 'APPEND_AT_CURSOR' else 0
    load_voice_browser_item(
        item,
        actor=active_arm,
        context=context,
        at_frame=_at_frame,
        strip_props=_voice_dialog_strip_props(
            item.voiceLineId,
            text=getattr(item, "text", ""),
            speaker=getattr(item, "speaker", ""),
            source="voice_browser_w2" if getattr(item, "game", VOICE_GAME_W3) == VOICE_GAME_W2 else "voice_browser",
            context=context,
            source_path=getattr(item, "source_path", ""),
        ),
    )


def has_invalid_surrogates(s):
    # Surrogate range: 0xD800 - 0xDFFF
    for char in s:
        if 0xD800 <= ord(char) <= 0xDFFF:
            return True
    return False
def _load_voice_name_map():
    candidate_paths = []

    try:
        res_dir = Path(__file__).resolve().parents[1]
        candidate_paths.append(str(res_dir / "CR2W" / "data" / "voice_names.json"))
    except Exception:
        pass

    dev_override_path = get_dev_override("voice_names_json", "")
    if dev_override_path:
        candidate_paths.append(dev_override_path)

    for voice_json_path in candidate_paths:
        try:
            with open(voice_json_path, 'r', encoding='utf-8') as json_file:
                data = json.load(json_file)
                if isinstance(data, dict):
                    return data
        except Exception:
            continue
    return {}


def _looks_like_group_tag(value: str) -> bool:
    v = (value or "").strip().lower()
    if not v:
        return False
    prefixes = ("group", "grp", "scene", "section", "part", "line", "set", "block", "node", "choice", "variant", "state", "phase")
    for prefix in prefixes:
        if v == prefix:
            return True
        if v.startswith(prefix) and v[len(prefix):].isdigit():
            return True
    return False


def _looks_like_campaign_tag(value: str) -> bool:
    return (value or "").strip().lower() in {"bob", "ep1"}


def _format_speaker_name(value: str) -> str:
    cleaned = (value or "").strip().replace("_", " ")
    if not cleaned:
        return ""
    return " ".join(part.capitalize() for part in cleaned.split())


def _fallback_speaker_from_csv_row(row: dict) -> str:
    cat1 = (row.get("CAT1") or "").strip()
    cat2 = (row.get("CAT2") or "").strip()
    cat3 = (row.get("CAT3") or "").strip()
    candidates = [cat2, cat1, cat3]

    for candidate in candidates:
        if not candidate:
            continue
        if _looks_like_group_tag(candidate):
            continue
        if _looks_like_campaign_tag(candidate):
            continue
        return _format_speaker_name(candidate)

    for candidate in candidates:
        if candidate and not _looks_like_group_tag(candidate):
            return _format_speaker_name(candidate)
    return ""


def _resolve_dialogue_speaker(line_id, voiceover=""):
    try:
        from ..strings_browser import strings_sources

        speaker = strings_sources.resolve_speaker(line_id, voiceover)
    except Exception:
        speaker = ""
    return str(speaker or "").strip().upper()


def _w2_source_path_for_entry(entry):
    bundle = getattr(entry, "bundle", None)
    return str(getattr(bundle, "ArchiveAbsolutePath", "") or "")


def _setup_w2_node_data(do_reload_strings=False, reload_scene_metadata=False):
    global _voice_node_cache, _voice_cache_loaded, _voice_filtered_indices, _voice_cache_identity_loaded

    text_language = dialog_language.get_active_text_language(bpy.context)
    voice_language = dialog_language.get_active_voice_language(bpy.context)
    try:
        from ..CR2W.witcher_cache.W2Speech import LoadWitcher2SpeechManager, w2_voice_base_name
        from ..CR2W.witcher_cache.W2Strings import LoadWitcher2StringsManager
    except Exception:
        log.warning("Witcher 2 dialogue managers are unavailable.", exc_info=True)
        _voice_node_cache = []
        _voice_filtered_indices = []
        _voice_cache_loaded = False
        _voice_cache_identity_loaded = None
        return

    speech_manager = LoadWitcher2SpeechManager(do_reload=do_reload_strings, language=voice_language)
    strings_manager = LoadWitcher2StringsManager(do_reload=do_reload_strings, language=text_language)
    scene_metadata = None
    try:
        from ..CR2W.witcher_cache.SceneDialog.w2_scene_dialog import LoadWitcher2SceneDialogMetadata

        scene_metadata = LoadWitcher2SceneDialogMetadata(do_reload=reload_scene_metadata)
    except Exception:
        log.warning("Witcher 2 scene dialogue metadata is unavailable.", exc_info=True)
    scene_line_summaries = None
    if scene_metadata is not None and hasattr(scene_metadata, "preload_line_summaries"):
        try:
            scene_line_summaries = scene_metadata.preload_line_summaries()
        except Exception:
            log.debug("Failed to preload W2 dialogue line summaries.", exc_info=True)
            scene_line_summaries = None
    effective_voice_language = dialog_language.normalize_dialog_language(
        getattr(speech_manager, "Language", "") or voice_language or "en"
    )
    effective_text_language = dialog_language.normalize_dialog_language(
        getattr(strings_manager, "Language", "") or text_language or "en"
    )

    _voice_node_cache = []
    _voice_filtered_indices = []

    items = getattr(speech_manager, "Items", None) or {}
    id_to_key = getattr(strings_manager, "IdToKey", {}) or {}
    idx = 0
    def _sort_voice_id(value):
        return (0, int(value)) if str(value).isdigit() else (1, str(value))

    for voice_id in sorted(items.keys(), key=_sort_voice_id):
        entries = items.get(voice_id) or []
        entry = entries[-1] if isinstance(entries, list) and entries else entries
        if entry is None:
            continue
        try:
            line_id_int = int(voice_id)
        except (TypeError, ValueError):
            continue

        text = strings_manager.GetString(line_id_int)
        text = "" if text is None else str(text)
        if has_invalid_surrogates(text):
            text = "ERROR READING"
        line_id = str(line_id_int)
        voice_name = w2_voice_base_name(line_id_int)
        string_key = str(id_to_key.get(line_id_int, "") or "")
        line_info = (
            scene_line_summaries.get(line_id, {})
            if scene_line_summaries is not None
            else scene_metadata.get_line(line_id) if scene_metadata else {}
        )
        speaker = str(line_info.get("speaker", "") or "").strip().upper()
        speaker = speaker or _resolve_dialogue_speaker(line_id_int, "") or "UNKN"
        speaker_candidates = [
            str(item.get("name", "") or "").strip().upper()
            for item in (line_info.get("speakers", []) or [])
            if isinstance(item, dict) and str(item.get("name", "") or "").strip()
        ]
        if speaker and speaker not in speaker_candidates:
            speaker_candidates.insert(0, speaker)
        scene_path = str(line_info.get("scene_path", "") or "")
        source_scenes = [
            str(path or "").replace("/", "\\")
            for path in (line_info.get("source_scenes", []) or [])
            if str(path or "").strip()
        ]
        if scene_path and scene_path not in source_scenes:
            source_scenes.insert(0, scene_path)
        entity_path = str(line_info.get("entity_path", "") or "")
        duration_value = getattr(entry, "duration", None)
        duration = ""
        if duration_value is not None:
            try:
                duration = str(round(float(duration_value), 2))
            except Exception:
                duration = str(duration_value)

        display_id = voice_name or line_id
        display_full = "{} [{}] {}{}".format(
            display_id,
            speaker,
            text,
            f" |{duration}" if duration else "",
        )
        display_compact = "[{}] {}".format(speaker, text) if text else "[{}] {}".format(speaker, display_id)
        search_blob = "{} {} {} {} {} {} {} {} {}".format(
            line_id,
            display_id.lower(),
            " ".join(s.lower() for s in speaker_candidates) or speaker.lower(),
            string_key.lower(),
            text.lower(),
            str(duration),
            "w2",
            " ".join(path.lower() for path in source_scenes),
            entity_path.lower(),
        )

        node = _make_voice_node(
            game=VOICE_GAME_W2,
            name=display_full,
            selfIndex=idx,
            parentIndex=-1,
            childCount=0,
            voiceLineId=line_id,
            speaker=speaker,
            line_id=line_id,
            duration=duration,
            text=text,
            display_full=display_full,
            display_compact=display_compact,
            search_blob=search_blob,
            text_language=effective_text_language,
            voice_language=effective_voice_language,
            source_path=_w2_source_path_for_entry(entry),
            speaker_candidates=speaker_candidates,
            scene_path=scene_path,
            source_scenes=source_scenes,
            entity_path=entity_path,
        )
        _voice_node_cache.append(node)
        idx += 1

    log.debug("++++ SetupNodeData W2 ++++")
    log.debug("Node count: %d", len(_voice_node_cache))
    _refresh_speaker_stats(_voice_node_cache)
    _voice_cache_loaded = bool(_voice_node_cache)
    _voice_cache_identity_loaded = _voice_cache_identity(bpy.context) if _voice_cache_loaded else None
    if _voice_node_cache:
        _save_voice_cache(bpy.context)


def SetupNodeData(do_reload_strings=False, reload_scene_metadata=False):
    global _voice_node_cache, _voice_cache_loaded, _voice_filtered_indices, _voice_cache_identity_loaded
    if get_active_voice_game(bpy.context) == VOICE_GAME_W2:
        _setup_w2_node_data(
            do_reload_strings=do_reload_strings,
            reload_scene_metadata=reload_scene_metadata,
        )
        return

    text_language = dialog_language.get_active_text_language(bpy.context)
    voice_language = dialog_language.get_active_voice_language(bpy.context)
    index_language = _voice_browser_index_language()
    dialog_language.set_active_dialog_languages(
        text_language=text_language,
        voice_language=voice_language,
        reset_string_manager=do_reload_strings,
    )
    speech_manager = LoadSpeechManager(do_reload=do_reload_strings, language=index_language)
    effective_voice_language = index_language
    if not getattr(speech_manager, "Items", None) and index_language != "en":
        log.warning(
            "No %s speech resources found; Dialogue Browser is using English speech ids with %s text.",
            index_language.upper(),
            text_language.upper(),
        )
        speech_manager = LoadSpeechManager(do_reload=do_reload_strings, language="en")
        effective_voice_language = "en"
    strings_manager = LoadStringsManager(do_reload = do_reload_strings)
    voice_data = _load_voice_name_map()
    voiceList = VoiceLineResourceManager().Get()
    scene_metadata = None
    try:
        from ..CR2W.witcher_cache.SceneDialog.w3_scene_dialog import LoadWitcher3SceneDialogMetadata

        scene_metadata = LoadWitcher3SceneDialogMetadata(do_reload=reload_scene_metadata)
    except Exception:
        log.warning("Witcher 3 scene dialogue metadata is unavailable.", exc_info=True)
    scene_line_summaries = None
    if scene_metadata is not None and hasattr(scene_metadata, "preload_line_summaries"):
        try:
            scene_line_summaries = scene_metadata.preload_line_summaries()
        except Exception:
            log.debug("Failed to preload W3 dialogue line summaries.", exc_info=True)
            scene_line_summaries = None
    
    _voice_node_cache = []
    _voice_filtered_indices = []
    
    char_dict = {
        'ciri' : 'Ciri',
        'yenn' : 'Yenn',
        'tris' : 'Triss',
        'grlt' : 'Geralt',
        'shni' : 'Shani',
        'anhe' : 'Henrietta',
        'syan': 'Syanna'
    }
    
    idx = 0
    for (voice_id, item) in speech_manager.Items.items():
        item = item[0]
        
        text = strings_manager.GetString(int(item.name))
        text = "ERROR READING" if text == None or has_invalid_surrogates(text) else text
        voice_id_str = str(voice_id)
        line_info = (
            scene_line_summaries.get(voice_id_str, {})
            if scene_line_summaries is not None
            else scene_metadata.get_line(voice_id_str) if scene_metadata else {}
        )
        character_name = str(line_info.get("speaker", "") or "").strip()
        if not character_name:
            character_name = _resolve_dialogue_speaker(voice_id_str, "")
        if not character_name:
            character_name = voice_data.get(voice_id_str)
        if not character_name:
            character_name = voiceList.SpeakerById.get(voice_id_str, "")
        
        character_name = char_dict.get(character_name) if character_name in char_dict else character_name
        
        speaker = character_name.upper() if character_name else 'UNKN'
        speaker_candidates = [
            str(item.get("name", "") or item.get("voicetag", "") or "").strip().upper()
            for item in (line_info.get("speakers", []) or [])
            if isinstance(item, dict) and str(item.get("name", "") or item.get("voicetag", "") or "").strip()
        ]
        if speaker and speaker not in speaker_candidates:
            speaker_candidates.insert(0, speaker)
        scene_path = str(line_info.get("scene_path", "") or "")
        source_scenes = [
            str(path or "").replace("/", "\\")
            for path in (line_info.get("source_scenes", []) or [])
            if str(path or "").strip()
        ]
        if scene_path and scene_path not in source_scenes:
            source_scenes.insert(0, scene_path)
        entity_path = str(line_info.get("entity_path", "") or "")
        voicetag = str(line_info.get("voicetag", "") or "")
        speaker_code = str(line_info.get("id", "") or voice_data.get(voice_id_str, "") or "")
        duration = round(item.duration, 2)
        display_full = "{} [{}] {} |{}".format(voice_id_str, speaker, text, duration)
        display_compact = "[{}] {}".format(speaker, text)
        # search_blob: voice_id + speaker + full dialogue text + speaker-lower (for partial name search)
        # Keep lower-cased so all matching is case-insensitive substring search
        speaker_lower = speaker.lower()
        search_blob = "{} {} {} {} {} {} {} {} {}".format(
            voice_id_str,
            speaker_lower,
            " ".join(s.lower() for s in speaker_candidates),
            str(speaker_code).lower(),
            voicetag.lower(),
            text.lower(),
            str(duration),
            " ".join(path.lower() for path in source_scenes),
            entity_path.lower(),
        )
        
        node = _make_voice_node(
            game=VOICE_GAME_W3,
            name=display_full,
            selfIndex=idx,
            parentIndex=-1,
            childCount=0,
            voiceLineId=voice_id_str,
            speaker=speaker,
            line_id=voice_id_str,
            duration=str(duration),
            text=text,
            display_full=display_full,
            display_compact=display_compact,
            search_blob=search_blob,
            text_language=text_language,
            voice_language=effective_voice_language,
            source_path=scene_path,
            speaker_candidates=speaker_candidates,
            scene_path=scene_path,
            source_scenes=source_scenes,
            entity_path=entity_path,
        )
        _voice_node_cache.append(node)
        idx += 1
        
    # calculate childCount for all nodes
    for node in _voice_node_cache:
        pi = node.get('parentIndex', -1)
        if pi != -1 and 0 <= pi < len(_voice_node_cache):
            _voice_node_cache[pi]['childCount'] = _voice_node_cache[pi].get('childCount', 0) + 1
            
    log.debug("++++ SetupNodeData ++++")
    log.debug("Node count: %d", len(_voice_node_cache))
    _refresh_speaker_stats(_voice_node_cache)
    _voice_cache_loaded = bool(_voice_node_cache)
    _voice_cache_identity_loaded = _voice_cache_identity(bpy.context) if _voice_cache_loaded else None
    
    # Persist to addon-managed JSON file
    if _voice_node_cache:
        _save_voice_cache(bpy.context)
        

def NewListItem( voiceList, node):
    item = voiceList.add()
    # node may be a dict (from _voice_node_cache)
    if isinstance(node, dict):
        _set_list_item_from_node(item, node)
    else:
        item.game = getattr(node, 'game', VOICE_GAME_W3)
        item.name = node.display_full or node.name
        item.nodeIndex = node.selfIndex
        item.childCount = node.childCount
        item.voiceLineId = node.voiceLineId
        item.speaker = node.speaker
        item.line_id = node.line_id
        item.duration = node.duration
        item.text = node.text
        item.display_full = node.display_full
        item.display_compact = node.display_compact
        item.source_path = getattr(node, 'source_path', '')
        item.speaker_candidates = getattr(node, 'speaker_candidates', '')
        item.scene_path = getattr(node, 'scene_path', '')
        item.entity_path = getattr(node, 'entity_path', '')
    return item


def SetupListFromNodeData():
    ensure_voice_cache(bpy.context)
    _refresh_speaker_stats(_voice_node_cache)
    if bpy.context and getattr(bpy.context, "scene", None):
        try:
            bpy.context.scene.witcher_voice_page_index = 0
        except Exception:
            pass
    _apply_voice_filter(bpy.context)


def _clear_voice_browser_runtime_state(scene=None):
    global _voice_node_cache, _voice_cache_loaded, _voice_filtered_indices, _voice_cache_identity_loaded
    _voice_node_cache = []
    _voice_filtered_indices = []
    _voice_cache_loaded = False
    _voice_cache_identity_loaded = None
    if scene is None:
        return
    if hasattr(scene, "witcher_voice_list"):
        scene.witcher_voice_list.clear()
    if hasattr(scene, "witcher_voice_list_index"):
        scene.witcher_voice_list_index = -1
    if hasattr(scene, "witcher_voice_selected_id"):
        scene.witcher_voice_selected_id = ""
    if hasattr(scene, "witcher_voice_previous_selected_id"):
        scene.witcher_voice_previous_selected_id = ""
    if hasattr(scene, "witcher_voice_filter_anchor_id"):
        scene.witcher_voice_filter_anchor_id = ""
    if hasattr(scene, "witcher_voice_show_all_voicetag_entities"):
        scene.witcher_voice_show_all_voicetag_entities = False
    if hasattr(scene, "witcher_voice_associated_paths"):
        scene.witcher_voice_associated_paths.clear()
    if hasattr(scene, "witcher_voice_associated_paths_key"):
        scene.witcher_voice_associated_paths_key = ""


def refresh_voice_browser_from_game_data(context, *, reload_scene_metadata=False):
    scene = getattr(context, "scene", None) if context is not None else None
    selected_id = _get_selected_voice_id(scene) if scene is not None else ""
    wm = getattr(context, "window_manager", None) if context is not None else None
    progress_started = False
    if wm is not None:
        try:
            wm.progress_begin(0, 100)
            wm.progress_update(5)
            progress_started = True
        except Exception:
            progress_started = False

    try:
        _clear_voice_browser_runtime_state(scene)
        try:
            from ..strings_browser import strings_sources as _strings_sources
            _strings_sources.cache_clear()
        except Exception:
            log.debug("Failed to clear strings browser cache during dialogue refresh.", exc_info=True)
        SetupNodeData(
            do_reload_strings=True,
            reload_scene_metadata=reload_scene_metadata,
        )
        if progress_started:
            wm.progress_update(85)
        if not _voice_node_cache:
            _clear_voice_browser_runtime_state(scene)
            return 0
        if scene is not None:
            if selected_id:
                _set_selected_voice_id(scene, selected_id)
            _apply_voice_filter(context)
            if selected_id:
                _refresh_voice_page(scene, selected_id=selected_id, jump_to_selected=True)
        if progress_started:
            wm.progress_update(100)
    finally:
        if progress_started:
            try:
                wm.progress_end()
            except Exception:
                pass
    return len(_voice_node_cache)


def clear_user_scene_dialog_index(context):
    game = get_active_voice_game(context)
    removed = False
    try:
        from ..CR2W.witcher_cache.SceneDialog.scene_dialog_index import ClearUserSceneDialogIndexMetadata

        removed = bool(ClearUserSceneDialogIndexMetadata(game))
    except Exception:
        log.warning("Failed to clear user scene dialogue index.", exc_info=True)
        raise

    try:
        if game == VOICE_GAME_W2:
            from ..CR2W.witcher_cache.SceneDialog.w2_scene_dialog import ClearWitcher2SceneDialogMetadataCache

            ClearWitcher2SceneDialogMetadataCache()
        else:
            from ..CR2W.witcher_cache.SceneDialog.w3_scene_dialog import ClearWitcher3SceneDialogMetadataCache

            ClearWitcher3SceneDialogMetadataCache()
    except Exception:
        log.debug("Failed to clear game scene dialogue metadata cache.", exc_info=True)
    return removed


def refresh_voice_dialog_language(context, refresh_audio=False):
    global _voice_node_cache, _voice_cache_loaded, _voice_filtered_indices, _voice_cache_identity_loaded

    scene = getattr(context, "scene", None) if context is not None else None
    selected_id = _get_selected_voice_id(scene) if scene is not None else ""
    text_language = dialog_language.get_active_text_language(context)
    try:
        from ..CR2W.witcher_cache.W3Strings.W3StringManager import W3StringManager
        current_string_language = dialog_language.normalize_dialog_language(getattr(W3StringManager.InstanceManager, "Language", "") or "")
    except Exception:
        current_string_language = ""
    dialog_language.set_active_dialog_languages(
        text_language=text_language,
        voice_language=dialog_language.get_active_voice_language(context),
        reset_string_manager=current_string_language != text_language,
    )
    _voice_node_cache = []
    _voice_filtered_indices = []
    _voice_cache_loaded = False
    _voice_cache_identity_loaded = None
    loaded_cache = _load_voice_cache(context)
    if not loaded_cache:
        SetupNodeData(do_reload_strings=True)
    if scene is not None:
        _apply_voice_filter(context)
        if selected_id:
            _refresh_voice_page(scene, selected_id=selected_id, jump_to_selected=True)
    return len(_voice_node_cache)

#
#   Inserts a new item into myVoiceList at position item_index
#   by copying data from node
#
def InsertBeneath( voiceList, parentIndex, parentIndent, node):
    after_index =parentIndex + 1
    item = NewListItem(voiceList,node)
    item.indent = parentIndent+1
    item_index = len(voiceList) -1 #because add() appends to end.
    voiceList.move(item_index,after_index)


def IsChild( child_node_index, parent_node_index, node_list):
    if child_node_index == -1:
        log.warning("bad node index")
        return False
    
    child = node_list[child_node_index]
    if child.parentIndex == parent_node_index:
        return True
    return False

#
#   Operation to Expand a list item.
#
class MyVoiceListItem_Expand(bpy.types.Operator):
    bl_idname = "witcher.quick_voice_expand" #NOT SURE WHAT TO PUT HERE.
    bl_label = "Tool Name"
    
    button_id: IntProperty(default=0)

    def execute(self, context):
        item_index = self.button_id
        item_list = context.scene.witcher_voice_list
        item = item_list[item_index]
        item_indent = item.indent
        
        nodeIndex = item.nodeIndex
        
        ensure_voice_cache(context)
        
        log.debug("item: %s", item)
        if item.expanded:
            log.debug("=== Collapse Item %d ===", item_index)
            item.expanded = False
            
            nextIndex = item_index+1
            while True:
                if nextIndex >= len(item_list):
                    break
                if item_list[nextIndex].indent <= item_indent:
                    break
                item_list.remove(nextIndex)
        else:
            log.debug("=== Expand Item %d ===", item_index)
            item.expanded = True
            
            for n in _voice_node_cache:
                if nodeIndex == n.get('parentIndex', -1):
                    InsertBeneath(item_list, item_index, item_indent, n)
            
        return {'FINISHED'}
    


#check in radish dirs if string, wav and cr2w exist. If they do add it to voice list and make it avaliaible.
radish_dirs = [
    d for d in get_dev_override_list("voice_radish_dirs", []) if isinstance(d, str) and d
]
global_sound = None


def _voice_language_asset_dir(base_path, context=None, language=None):
    base_path = str(base_path or "").strip()
    base_dir = Path(base_path) if base_path else Path()
    language = dialog_language.normalize_dialog_language(language or dialog_language.get_active_voice_language(context))
    if base_path and language and language != "en":
        return base_dir / language
    return base_dir


def load_voice_and_lipsync(voiceLineId, actor = None, context = None, at_frame = 0, recreate_phonemes = None, strip_props = None, nla_mode = None):
    unpadded_line_id = ''+voiceLineId
    if context == None:
        context = bpy.context
    if recreate_phonemes is None:
        recreate_phonemes = getattr(context.scene, "witcher_voice_recreate_phonemes", False)
    target_armature = _resolve_voice_target_armature(context, actor=actor)
    namelen = len(voiceLineId)
    if namelen != 10:
        zeros = "0000000000"
        num_of_zeros = 10 - namelen
        voiceLineId = zeros[:num_of_zeros] + voiceLineId
    requested_voice_language = dialog_language.get_active_voice_language(context)
    language = requested_voice_language
    sound_directory_to_check: Path = _voice_language_asset_dir(get_W3_OGG_PATH(context), context, language=language)
    cr2w_directory_to_check: Path = _voice_language_asset_dir(get_W3_VOICE_PATH(context), context, language=language)
    if str(get_W3_OGG_PATH(context) or "").strip():
        sound_directory_to_check.mkdir(parents=True, exist_ok=True)
    if str(get_W3_VOICE_PATH(context) or "").strip():
        cr2w_directory_to_check.mkdir(parents=True, exist_ok=True)
    
    soundPath: Path = sound_directory_to_check / f"{voiceLineId}.ogg"
    soundPath_wav: Path = sound_directory_to_check / f"{voiceLineId}.wav"
    cr2wPath: Path = cr2w_directory_to_check / f"{voiceLineId}.cr2w"
    wemPath: Path = cr2w_directory_to_check / f"{voiceLineId}.wem"
    
    
    ##? RADISH CHECKING
    if not cr2wPath.is_file():
        for dir in radish_dirs:
            dir = Path(dir) / f"speech/speech.{language}.wem"
            files = Path(dir).glob('*')
            for file in files:
                if file.suffix == ".cr2w" and unpadded_line_id in file.stem:
                    log.debug("Found speech file: %s", file.stem)
                    cr2wPath = file
                    break
        #check radish dirs
    
    if cr2wPath.is_file() and not soundPath.is_file():
        path = cr2wPath
        folder = path.parent.name
        if "speech." in folder and ".wem" in folder and "lipsyncanim" in cr2wPath.stem:
            speechId = cr2wPath.stem.split('.')[0]
            soundFolder = str(path.parent.parent)+"\\"+path.parent.name.replace('wem','wav')
            if os.path.isdir(soundFolder):
                files = Path(soundFolder).glob('*')
                for file in files:
                    if file.suffix == ".wav" and speechId in file.stem:
                        soundPath = file
                        break
    ##? RADISH CHECKING
    
    if not cr2wPath.is_file():
        speech_manager = LoadSpeechManager(language=language)
        speech_matches = speech_manager.find_item_by_hash(unpadded_line_id) or []
        if not speech_matches and language != "en":
            fallback_manager = LoadSpeechManager(language="en")
            fallback_matches = fallback_manager.find_item_by_hash(unpadded_line_id) or []
            if fallback_matches:
                log.warning(
                    "Voice line %s was not found in %s speech resources; loading English audio/lipsync fallback.",
                    unpadded_line_id,
                    requested_voice_language.upper(),
                )
                language = "en"
                sound_directory_to_check = _voice_language_asset_dir(get_W3_OGG_PATH(context), context, language=language)
                cr2w_directory_to_check = _voice_language_asset_dir(get_W3_VOICE_PATH(context), context, language=language)
                if str(get_W3_OGG_PATH(context) or "").strip():
                    sound_directory_to_check.mkdir(parents=True, exist_ok=True)
                if str(get_W3_VOICE_PATH(context) or "").strip():
                    cr2w_directory_to_check.mkdir(parents=True, exist_ok=True)
                soundPath = sound_directory_to_check / f"{voiceLineId}.ogg"
                soundPath_wav = sound_directory_to_check / f"{voiceLineId}.wav"
                cr2wPath = cr2w_directory_to_check / f"{voiceLineId}.cr2w"
                wemPath = cr2w_directory_to_check / f"{voiceLineId}.wem"
                speech_matches = fallback_matches
        if not speech_matches:
            raise FileNotFoundError(f"Voice line {unpadded_line_id} was not found in {language.upper()} speech resources")
        item:SpeechEntry = speech_matches[0]
        item.extract_to_file(str(item.id), output_dir=str(cr2w_directory_to_check))

    if cr2wPath.is_file():
        log.info('Importing Lipsync')
        _mode_map = {'REPLACE': 'replace', 'APPEND': 'append', 'APPEND_AT_CURSOR': 'append_at_cursor'}
        _nla_mode = nla_mode or _mode_map.get(getattr(context.scene, 'witcher_anim_nla_mode', 'REPLACE'), 'replace')
        import_anims.import_lipsync(
            context,
            str(cr2wPath),
            use_NLA=True,
            NLA_track="voice_import",
            override_select=target_armature if target_armature else actor,
            at_frame=at_frame,
            nla_mode=_nla_mode,
        )
        # Ensure the newly created NLA track evaluates during morph sampling.
        _actor_arm = target_armature
        if _actor_arm and getattr(_actor_arm, 'type', None) == 'ARMATURE':
            if _actor_arm.animation_data:
                _actor_arm.animation_data.use_nla = True
        if recreate_phonemes:
            armature = target_armature
            if armature is None:
                raise RuntimeError(
                    "Recreate Phonemes failed: no character armature found. "
                    "Set a character target or select an armature."
                )
            # Will raise RuntimeError with a descriptive message on failure.
            _recreate_phonemes_from_lipsync(context, armature, voiceLineId, track_name="voice_import")

    if not soundPath.is_file() and not soundPath_wav.is_file():
        vgmstream_path = get_vgmstream_path(context)
        output_folder = str(sound_directory_to_check)
        if wemPath.is_file() and os.path.isfile(vgmstream_path):
            if not output_folder:
                output_folder = bpy.app.tempdir
            os.makedirs(output_folder, exist_ok=True)

            output_wav = os.path.join(output_folder, os.path.basename(str(wemPath)).replace('.wem', '.wav'))
            command = [vgmstream_path, "-o", output_wav, str(wemPath)]

            try:
                subprocess.run(command, check=True)
                # Here you might want to add the WAV to Blender's sequencer
            except subprocess.CalledProcessError as e:
                log.error("vgmstream conversion failed: %s", e)
        elif wemPath.is_file():
            log.warning(
                "Extracted %s audio for voice line %s, but no WAV/OGG was created because vgmstream is not configured: %s",
                language.upper(),
                unpadded_line_id,
                vgmstream_path or "<unset>",
            )
        
    if soundPath.is_file() or soundPath_wav.is_file():
        if not soundPath.is_file() and soundPath_wav.is_file():
            soundPath = soundPath_wav
        log.info('Importing Sound')
        scene = context.scene 

        if not scene.sequence_editor:
            scene.sequence_editor_create()
        strips = _get_sequence_editor_strips(scene.sequence_editor)
        if strips is None:
            raise RuntimeError("Blender sequence editor strips API is unavailable")

        if getattr(scene, "witcher_voice_replace_audio", False):
            sound_strips = [strip for strip in strips if strip.type == 'SOUND']
            for strip in sound_strips:
                strips.remove(strip)

        # try:
        #     soundstrip = scene.sequence_editor.sequences.new_sound("voiceline", str(soundPath), 1, at_frame)
        # except Exception as e:
        channel = 1 if getattr(scene, "witcher_voice_replace_audio", False) else _get_next_sound_channel(scene)
        soundstrip = strips.new_sound(
            soundPath.stem,
            str(soundPath),
            channel=channel,
            frame_start= math.ceil(at_frame)+1
        )
        soundstrip.frame_start = at_frame
        tag_props = _merge_voice_dialog_strip_props(unpadded_line_id, strip_props=strip_props, context=context)
        tag_props[dialog_language.DIALOG_AUDIO_LANGUAGE_PROP] = language
        for prop_name, prop_value in tag_props.items():
            try:
                soundstrip[prop_name] = prop_value
            except Exception:
                log.debug("Could not tag sound strip %s with %s", getattr(soundstrip, "name", ""), prop_name, exc_info=True)
        # Only extend frame_end, never shrink it
        strip_end = int(math.ceil(soundstrip.frame_final_end))
        if strip_end > scene.frame_end:
            scene.frame_end = strip_end
        return soundstrip
    return None


def load_w2_voice_and_lipsync(voiceLineId, actor=None, context=None, at_frame=0, strip_props=None, nla_mode=None):
    if context is None:
        context = bpy.context
    target_armature = _resolve_voice_target_armature(context, actor=actor)
    try:
        from . import ui_file_browser, ui_speech
    except Exception as exc:
        raise RuntimeError(f"Witcher 2 voice import helpers are unavailable: {exc}") from exc

    extracted_path = ui_file_browser.ensure_witcher2_speech_item_extracted(context, str(voiceLineId), overwrite=False)
    if not extracted_path:
        raise FileNotFoundError(f"Witcher 2 voice line {voiceLineId} was not found in speech resources")

    dat_path, mp2_path, soundstrip = ui_speech._import_w2_voice_pair(
        context,
        extracted_path,
        active_armature=target_armature,
        use_nla=True,
    )

    if soundstrip is not None:
        line_id = str(voiceLineId or "").strip()
        tag_props = dict(strip_props or {})
        tag_props.setdefault(dialog_language.DIALOG_SUBTITLE_SOURCE_PROP, "voice_browser_w2")
        tag_props[dialog_language.DIALOG_SUBTITLE_SOURCE_PATH_PROP] = mp2_path or dat_path or extracted_path
        tag_props = _merge_voice_dialog_strip_props(line_id, strip_props=tag_props, context=context)
        audio_language = ""
        try:
            audio_language = ui_speech._w2_voice_language_from_path(mp2_path or dat_path or extracted_path)
        except Exception:
            audio_language = ""
        tag_props[dialog_language.DIALOG_AUDIO_LANGUAGE_PROP] = (
            audio_language or dialog_language.get_active_voice_language(context)
        )
        for prop_name, prop_value in tag_props.items():
            try:
                soundstrip[prop_name] = prop_value
            except Exception:
                log.debug("Could not tag W2 sound strip %s with %s", getattr(soundstrip, "name", ""), prop_name, exc_info=True)
    return soundstrip


def load_voice_browser_item(item_or_line_id, actor=None, context=None, at_frame=0, strip_props=None, nla_mode=None):
    if context is None:
        context = bpy.context
    if hasattr(item_or_line_id, "voiceLineId"):
        line_id = getattr(item_or_line_id, "voiceLineId", "")
        game = getattr(item_or_line_id, "game", "") or get_active_voice_game(context)
    else:
        line_id = str(item_or_line_id or "")
        game = get_active_voice_game(context)

    if str(game or "").upper() == VOICE_GAME_W2:
        return load_w2_voice_and_lipsync(
            line_id,
            actor=actor,
            context=context,
            at_frame=at_frame,
            strip_props=strip_props,
            nla_mode=nla_mode,
        )

    return load_voice_and_lipsync(
        line_id,
        actor=actor,
        context=context,
        at_frame=at_frame,
        strip_props=strip_props,
        nla_mode=nla_mode,
    )


class MyVoiceListItem_Debug(bpy.types.Operator):
    bl_idname = "witcher.quick_voice_debug"
    bl_label = "Dialogue Browser Action"
    
    @classmethod
    def description(cls, context, properties):
        if properties.action == "refresh":
            return "Refresh speech and string data, then rebuild the visible dialogue list"
        if properties.action in {"reset3", "rebuild_cache"}:
            return "Rebuild the user dialogue browser cache using the shipped scene index"
        if properties.action == "rebuild_scene_metadata":
            return "Scan raw scene files and rebuild user scene metadata"
        if properties.action == "clear_scene_index":
            return "Remove the user scene metadata overlay"
        if properties.action == "load":
            return "Load the selected line onto the character/armature"
        if properties.action == "clear":
            return "Clear the current dialogue list"
        return ""

    action: StringProperty(default="default")

    def invoke(self, context, event):
        if self.action in {"rebuild_cache", "rebuild_scene_metadata", "clear_scene_index"}:
            return context.window_manager.invoke_confirm(self, event)
        return self.execute(context)

    def draw(self, context):
        layout = self.layout
        if self.action == "rebuild_scene_metadata":
            layout.label(text="Scan raw scene files.")
            layout.label(text="This can take a long time.")
            layout.label(text="Shipped SQLite DBs stay read-only.")
        elif self.action == "rebuild_cache":
            layout.label(text="Rebuild the user voice cache.")
            layout.label(text="Uses shipped scene indexes.")
            layout.label(text="Good after adding DLC strings.")
        elif self.action == "clear_scene_index":
            layout.label(text="Remove user scene metadata.")
            layout.label(text="Shipped SQLite DBs stay untouched.")
    
    def execute(self, context):
        global _voice_filtered_indices
        scene = context.scene
        action = self.action
        if "load" == action:
            
            if scene.witcher_voice_list_index >= 0 and scene.witcher_voice_list:
                item = scene.witcher_voice_list[scene.witcher_voice_list_index]

                # Auto-load face morphs if the active armature needs them
                active_arm = _get_active_armature(context)
                if active_arm and not _armature_has_face_morphs(active_arm):
                    _auto_load_face_morphs(context, active_arm)

                filename = item.voiceLineId
                _at_frame = context.scene.frame_current if getattr(context.scene, 'witcher_anim_nla_mode', 'REPLACE') == 'APPEND_AT_CURSOR' else 0
                try:
                    soundstrip = load_voice_browser_item(
                        item,
                        actor=active_arm,
                        context=context,
                        at_frame=_at_frame,
                        strip_props=_voice_dialog_strip_props(
                            item.voiceLineId,
                            text=getattr(item, "text", ""),
                            speaker=getattr(item, "speaker", ""),
                            source="voice_browser_w2" if getattr(item, "game", VOICE_GAME_W3) == VOICE_GAME_W2 else "voice_browser",
                            context=context,
                            source_path=getattr(item, "source_path", ""),
                        ),
                    )
                except Exception as exc:
                    self.report({'ERROR'}, f"Failed to load voice line {filename}: {exc}")
                    return {'CANCELLED'}
                if soundstrip is None:
                    self.report({'WARNING'}, f"No playable audio was imported for voice line {filename}; check vgmstream and language speech resources.")
                else:
                    active_voice_language = dialog_language.get_active_voice_language(context)
                    audio_language = str(soundstrip.get(dialog_language.DIALOG_AUDIO_LANGUAGE_PROP, active_voice_language) or active_voice_language)
                    if dialog_language.normalize_dialog_language(audio_language) != dialog_language.normalize_dialog_language(active_voice_language):
                        self.report({'WARNING'}, f"Loaded {audio_language.upper()} audio because {active_voice_language.upper()} speech was not found for {filename}.")
                
        elif "refresh" == action:
            try:
                count = refresh_voice_browser_from_game_data(context, reload_scene_metadata=False)
            except Exception as exc:
                self.report({'ERROR'}, f"Failed to refresh dialogue list: {exc}")
                return {'CANCELLED'}
            self.report({'INFO'}, f"Refreshed dialogue list: {count} lines")
        elif action in {"reset3", "rebuild_cache", "rebuild_scene_metadata"}:
            log.debug("=== Voice Cache Rebuild ====")
            try:
                count = refresh_voice_browser_from_game_data(
                    context,
                    reload_scene_metadata=(action == "rebuild_scene_metadata"),
                )
            except Exception as exc:
                self.report({'ERROR'}, f"Failed to rebuild dialogue cache: {exc}")
                return {'CANCELLED'}
            if action == "rebuild_scene_metadata":
                self.report({'INFO'}, f"Rebuilt scene metadata and dialogue cache: {count} lines")
            else:
                self.report({'INFO'}, f"Rebuilt dialogue cache: {count} lines")
        elif "clear_scene_index" == action:
            try:
                removed = clear_user_scene_dialog_index(context)
            except Exception as exc:
                self.report({'ERROR'}, f"Failed to clear user scene index: {exc}")
                return {'CANCELLED'}
            _clear_voice_browser_runtime_state(scene)
            self.report({'INFO'}, "Removed user scene index" if removed else "No user scene index found")
        elif "clear" == action:
            log.debug("=== Debug Clear ====")
            _voice_filtered_indices = []
            bpy.context.scene.witcher_voice_list.clear()
            scene.witcher_voice_list_index = -1
            if hasattr(scene, "witcher_voice_page_index"):
                scene.witcher_voice_page_index = 0
            _set_selected_voice_id(scene, "")
            if hasattr(scene, "witcher_voice_show_all_voicetag_entities"):
                scene.witcher_voice_show_all_voicetag_entities = False
            if hasattr(scene, "witcher_voice_associated_paths"):
                scene.witcher_voice_associated_paths.clear()
            if hasattr(scene, "witcher_voice_associated_paths_key"):
                scene.witcher_voice_associated_paths_key = ""
        else:
            log.warning("unknown debug action: %s", action)

        return {'FINISHED'}


class MyVoiceList_ClearSceneDialogueAudio(bpy.types.Operator):
    bl_idname = "witcher.quick_voice_clear_scene_dialogue_audio"
    bl_label = "Clear Audio/Subtitles"
    bl_description = "Remove Witcher dialogue audio strips and subtitle metadata from the scene"
    bl_options = {'UNDO'}

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def draw(self, context):
        layout = self.layout
        layout.label(text="Remove dialogue audio strips.")
        layout.label(text="Subtitle data is on those strips.")
        layout.label(text="Extracted files are kept.")

    def execute(self, context):
        try:
            removed = clear_dialog_audio_and_subtitles(context.scene)
        except Exception as exc:
            self.report({'ERROR'}, f"Failed to clear dialogue audio: {exc}")
            return {'CANCELLED'}

        if removed:
            self.report({'INFO'}, f"Removed {removed} dialogue audio strip(s)")
        else:
            self.report({'INFO'}, "No dialogue audio strips found")
        return {'FINISHED'}


class MyVoiceList_Page(bpy.types.Operator):
    bl_idname = "witcher.quick_voice_page"
    bl_label = "Dialogue Page"
    bl_description = "Navigate dialogue pages"

    action: StringProperty(default="next")

    def execute(self, context):
        scene = context.scene
        if not _voice_filtered_indices and _voice_node_cache:
            _apply_voice_filter(context)
        stats = get_voice_browser_stats(scene)
        target = browser_core.page_target(
            self.action,
            stats["page_index"],
            stats["total_pages"],
        )
        if target is None:
            return {'CANCELLED'}
        scene.witcher_voice_page_index = target
        _refresh_voice_page(scene, selected_id=_get_selected_voice_id(scene))
        return {'FINISHED'}

class MyVoiceListItem_Copy(bpy.types.Operator):
    bl_idname = "witcher.quick_voice_copy"
    bl_label = "Copy Dialog"
    bl_description = "Copy the displayed line(s) to the clipboard"

    scope: StringProperty(default="selected")

    @classmethod
    def description(cls, context, properties):
        if properties.scope == "all":
            return "Copy all filtered lines to the clipboard"
        return "Copy the selected displayed line to the clipboard"

    def execute(self, context):
        scene = context.scene
        items = scene.witcher_voice_list

        lines = []
        if self.scope == "all":
            ensure_voice_cache(context)
            if _voice_filtered_indices:
                for cache_idx in _voice_filtered_indices:
                    if 0 <= cache_idx < len(_voice_node_cache):
                        lines.append(_node_display_text(scene, _voice_node_cache[cache_idx]))
            else:
                lines = [_get_display_text(scene, item) for item in items]
        else:
            if scene.witcher_voice_list_index < 0 or scene.witcher_voice_list_index >= len(items):
                self.report({'WARNING'}, "No dialog line selected")
                return {'CANCELLED'}
            lines = [_get_display_text(scene, items[scene.witcher_voice_list_index])]

        if not lines:
            self.report({'WARNING'}, "No dialog lines to copy")
            return {'CANCELLED'}

        context.window_manager.clipboard = "\n".join(lines)
        self.report({'INFO'}, f"Copied {len(lines)} line(s) to clipboard")
        return {'FINISHED'}

class MyVoiceList_ImportSpeakerEntity(bpy.types.Operator):
    bl_idname = "witcher.quick_voice_import_speaker_entity"
    bl_label = "Import Speaker Entity"
    bl_description = "Import the entity most associated with the selected speaker"

    @classmethod
    def poll(cls, context):
        item = _get_selected_voice_item(context)
        game = str(getattr(item, "game", VOICE_GAME_W3) or VOICE_GAME_W3).upper() if item else ""
        return bool(item and game in {VOICE_GAME_W2, VOICE_GAME_W3})

    @classmethod
    def description(cls, context, properties):
        item = _get_selected_voice_item(context)
        speaker = str(getattr(item, "speaker", "") or "").strip() if item else ""
        if speaker:
            return f"Import the entity most associated with {speaker}"
        return cls.bl_description

    def execute(self, context):
        item = _get_selected_voice_item(context)
        if item is None:
            self.report({'WARNING'}, "No dialog line selected")
            return {'CANCELLED'}
        entity = _selected_voice_entity_candidate(context)
        entity_path = _entity_dict_path(entity)
        if not entity_path:
            self.report({'WARNING'}, "No associated entity found for this speaker")
            return {'CANCELLED'}

        game = str(getattr(item, "game", VOICE_GAME_W3) or VOICE_GAME_W3).upper()
        abs_file_path = _resolve_voice_entity_path(context, entity_path, game)
        if not abs_file_path:
            self.report({'WARNING'}, f"Associated entity was not found in {game} data")
            return {'CANCELLED'}

        appearance = _entity_dict_appearance(entity)
        try:
            _import_voice_entity(context, abs_file_path, selected_appearance_name=appearance)
        except Exception as exc:
            log.error("Entity import failed for %s", abs_file_path, exc_info=True)
            self.report({'ERROR'}, f"Entity import failed: {exc}")
            return {'CANCELLED'}

        speaker = str(getattr(item, "speaker", "") or "").strip()
        label = f" for {speaker}" if speaker else ""
        self.report({'INFO'}, f"Imported speaker entity{label}")
        return {'FINISHED'}


class MyVoiceList_CopyAssociatedPath(bpy.types.Operator):
    bl_idname = "witcher.quick_voice_copy_associated_path"
    bl_label = "Copy Path"
    bl_description = "Copy this associated repo path"

    path: StringProperty(default="")

    @classmethod
    def description(cls, context, properties):
        path = str(getattr(properties, "path", "") or "")
        return path if path else cls.bl_description

    def execute(self, context):
        path = str(self.path or "").strip()
        if not path:
            self.report({'WARNING'}, "No path to copy")
            return {'CANCELLED'}
        context.window_manager.clipboard = path
        self.report({'INFO'}, f"Copied: {path}")
        return {'FINISHED'}


class MyVoiceList_OpenAssociatedPath(bpy.types.Operator):
    bl_idname = "witcher.quick_voice_open_associated_path"
    bl_label = "Open File Location"
    bl_description = "Open the folder containing this associated file"

    repo_path: StringProperty(default="")
    game: StringProperty(default=VOICE_GAME_W3)

    @classmethod
    def description(cls, context, properties):
        repo_path = str(getattr(properties, "repo_path", "") or "")
        return f"Show {repo_path} on disk" if repo_path else cls.bl_description

    def execute(self, context):
        repo_path = str(self.repo_path or "").strip()
        if not repo_path:
            self.report({'WARNING'}, "No path to open")
            return {'CANCELLED'}

        game = str(self.game or VOICE_GAME_W3).upper()
        disk_path = _resolve_or_extract_voice_extracted_repo_path(context, repo_path, game)
        if not disk_path:
            self.report({'WARNING'}, f"File not found: {repo_path}")
            return {'CANCELLED'}
        _set_associated_path_resolved(context, repo_path, game, disk_path)

        try:
            from ..CR2W.common_blender import win_safe_path, win_unprefix_path

            disk_path = win_unprefix_path(os.path.normpath(disk_path))
            safe_path = win_safe_path(disk_path)
        except Exception:
            disk_path = os.path.normpath(disk_path)
            safe_path = disk_path

        folder = disk_path if os.path.isdir(safe_path) else os.path.dirname(disk_path)
        if not folder:
            self.report({'WARNING'}, f"Could not find folder for: {repo_path}")
            return {'CANCELLED'}

        try:
            if os.path.isfile(safe_path) and os.name == 'nt':
                explorer_path = disk_path.replace('"', '\\"')
                subprocess.Popen(f'explorer.exe /select,"{explorer_path}"')
                selected_file = True
            else:
                bpy.ops.wm.path_open(filepath=folder)
                selected_file = False
        except Exception as exc:
            self.report({'ERROR'}, f"Failed to open location: {exc}")
            return {'CANCELLED'}

        self.report({'INFO'}, f"Selected: {disk_path}" if selected_file else f"Opened: {folder}")
        return {'FINISHED'}


class MyVoiceList_ImportAssociatedEntity(bpy.types.Operator):
    bl_idname = "witcher.quick_voice_import_associated_entity"
    bl_label = "Import Template"
    bl_description = "Extract and import this associated character template"

    repo_path: StringProperty(default="")
    game: StringProperty(default=VOICE_GAME_W3)
    appearance: StringProperty(default="")

    @classmethod
    def description(cls, context, properties):
        repo_path = str(getattr(properties, "repo_path", "") or "")
        appearance = str(getattr(properties, "appearance", "") or "")
        if repo_path and appearance:
            return f"Import {repo_path} as {appearance}"
        return f"Import {repo_path}" if repo_path else cls.bl_description

    def execute(self, context):
        repo_path = str(self.repo_path or "").strip()
        if not repo_path:
            self.report({'WARNING'}, "No template path to import")
            return {'CANCELLED'}
        if os.path.splitext(repo_path)[1].lower() != ".w2ent":
            self.report({'WARNING'}, f"Not a character template: {repo_path}")
            return {'CANCELLED'}

        game = str(self.game or VOICE_GAME_W3).upper()
        appearance = str(self.appearance or "").strip()
        disk_path = _resolve_or_extract_voice_extracted_repo_path(context, repo_path, game)
        if not disk_path:
            self.report({'WARNING'}, f"Template not found: {repo_path}")
            return {'CANCELLED'}
        _set_associated_path_resolved(context, repo_path, game, disk_path)

        try:
            _import_voice_entity(context, disk_path, selected_appearance_name=appearance)
        except Exception as exc:
            log.error("Associated template import failed for %s", disk_path, exc_info=True)
            self.report({'ERROR'}, f"Template import failed: {exc}")
            return {'CANCELLED'}

        suffix = f" ({appearance})" if appearance else ""
        self.report({'INFO'}, f"Imported: {repo_path}{suffix}")
        return {'FINISHED'}


class MyVoiceList_ImportAssociatedScene(bpy.types.Operator):
    bl_idname = "witcher.quick_voice_import_associated_scene"
    bl_label = "Import Scene"
    bl_description = "Extract and import this associated Witcher 3 scene"

    repo_path: StringProperty(default="")
    game: StringProperty(default=VOICE_GAME_W3)

    @classmethod
    def description(cls, context, properties):
        repo_path = str(getattr(properties, "repo_path", "") or "")
        return f"Import {repo_path}" if repo_path else cls.bl_description

    def execute(self, context):
        repo_path = str(self.repo_path or "").strip()
        if not repo_path:
            self.report({'WARNING'}, "No scene path to import")
            return {'CANCELLED'}
        if os.path.splitext(repo_path)[1].lower() != ".w2scene":
            self.report({'WARNING'}, f"Not a scene file: {repo_path}")
            return {'CANCELLED'}

        game = str(self.game or VOICE_GAME_W3).upper()
        if game != VOICE_GAME_W3:
            self.report({'WARNING'}, "Associated scene import is only enabled for Witcher 3")
            return {'CANCELLED'}

        disk_path = _resolve_or_extract_voice_extracted_repo_path(context, repo_path, game)
        if not disk_path:
            self.report({'WARNING'}, f"Scene not found: {repo_path}")
            return {'CANCELLED'}
        _set_associated_path_resolved(context, repo_path, game, disk_path)

        try:
            from ..importers import import_scene
            from .ui_scene import _w2scene_unload_active_cutscene_section, _w2scene_sync_loaded_state

            _w2scene_unload_active_cutscene_section(context)
            scene_importer = import_scene.import_w3_scene(disk_path)
            scene_importer.load_sections()
            _w2scene_sync_loaded_state(context.scene, disk_path, scene_importer=scene_importer)
            bpy.context.view_layer.update()
        except Exception as exc:
            log.error("Associated scene import failed for %s", disk_path, exc_info=True)
            self.report({'ERROR'}, f"Scene import failed: {exc}")
            return {'CANCELLED'}

        self.report({'INFO'}, f"Imported: {repo_path}")
        return {'FINISHED'}


class MyVoiceList_ToggleVoiceTagEntities(bpy.types.Operator):
    bl_idname = "witcher.quick_voice_toggle_voicetag_entities"
    bl_label = "Show VoiceTag Entities"
    bl_description = "Show or hide all templates associated with the selected line's voice tag"

    def execute(self, context):
        scene = context.scene
        current = bool(getattr(scene, "witcher_voice_show_all_voicetag_entities", False))
        scene.witcher_voice_show_all_voicetag_entities = not current
        refresh_selected_voice_associated_paths(context, force=True)
        return {'FINISHED'}


class MYVOICELISTITEM_UL_basic(bpy.types.UIList):
    use_filter_show = False
    use_filter_sort_alpha = False

    def draw_item(
        self, context, layout, data, item, icon,
        active_data, active_propname, index
    ):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            display_text = _get_display_text(context.scene, item)
            row = layout.row(align=True)
            line_id = str(getattr(item, "voiceLineId", "") or "")
            game = str(getattr(item, "game", VOICE_GAME_W3) or VOICE_GAME_W3)
            preview_op = row.operator(
                "witcher.strings_browser_preview_voice",
                text="",
                icon='CANCEL' if _is_dialogue_preview_playing(game, line_id) else 'PLAY',
            )
            preview_op.game = game
            preview_op.line_id = line_id
            preview_op.text = str(getattr(item, "text", "") or "")
            preview_op.speaker = str(getattr(item, "speaker", "") or "")
            row.label(text=display_text)
        else:
            layout.label(text=item.name)

    def draw_filter(self, context, layout):
        # Suppress Blender's built-in filter/sort bar entirely
        pass

    def filter_items(self, context, data, propname):
        return [], []



class MyVoiceList_ClearFilter(bpy.types.Operator):
    bl_idname = "witcher.quick_voice_clear_filter"
    bl_label = "Clear Filter"
    bl_description = "Clear search text, speaker filter, and scene filter"

    def execute(self, context):
        scene = context.scene
        _set_voice_filter_anchor(scene)
        scene.witcher_voice_search_text = ""
        scene.witcher_voice_speaker_filter = ""
        if hasattr(scene, "witcher_voice_scene_filter"):
            scene.witcher_voice_scene_filter = ""
        if hasattr(scene, "witcher_voice_page_index"):
            scene.witcher_voice_page_index = 0
        _apply_voice_filter(context)
        return {'FINISHED'}


class MyVoiceList_BackSelect(bpy.types.Operator):
    bl_idname = "witcher.quick_voice_back_select"
    bl_label = "Back Select"
    bl_description = "Return to the dialogue line selected before the last filter/list jump"

    def execute(self, context):
        scene = context.scene
        target = (
            str(getattr(scene, "witcher_voice_previous_selected_id", "") or "").strip()
            or str(getattr(scene, "witcher_voice_filter_anchor_id", "") or "").strip()
        )
        if not target:
            self.report({'WARNING'}, "No previous dialogue selection.")
            return {'CANCELLED'}

        ensure_voice_cache(context)
        if not _voice_filtered_indices and _voice_node_cache:
            _apply_voice_filter(context)

        if _filtered_voice_position(target) < 0:
            if hasattr(scene, "witcher_voice_search_text"):
                scene.witcher_voice_search_text = ""
            if hasattr(scene, "witcher_voice_speaker_filter"):
                scene.witcher_voice_speaker_filter = ""
            if hasattr(scene, "witcher_voice_scene_filter"):
                scene.witcher_voice_scene_filter = ""
            if hasattr(scene, "witcher_voice_page_index"):
                scene.witcher_voice_page_index = 0
            _set_voice_filter_anchor(scene, target)
            _apply_voice_filter(context)

        if _filtered_voice_position(target) < 0:
            self.report({'WARNING'}, f"Previous dialogue line {target} is not available.")
            return {'CANCELLED'}

        _refresh_voice_page(scene, selected_id=target, jump_to_selected=True)
        _clear_voice_filter_anchor(scene, target)
        self.report({'INFO'}, f"Selected dialogue line {target}.")
        return {'FINISHED'}


class MyVoiceList_FilterSpeaker(bpy.types.Operator):
    bl_idname = "witcher.quick_voice_filter_speaker"
    bl_label = "Filter Speaker"
    bl_description = "Filter list to only lines from this speaker"

    speaker: StringProperty(default="")
    count: IntProperty(default=0)

    @classmethod
    def description(cls, context, properties):
        if properties.speaker:
            if properties.count:
                return f"Filter to {properties.speaker} ({properties.count} lines)"
            return f"Filter list to only lines spoken by {properties.speaker}"
        return "Filter list to only lines from this speaker"

    def execute(self, context):
        _set_speaker_filter(context.scene, context, self.speaker)
        return {'FINISHED'}

class MyVoiceList_ClearSpeaker(bpy.types.Operator):
    bl_idname = "witcher.quick_voice_clear_speaker"
    bl_label = "Clear Speaker Filter"
    bl_description = "Remove the current speaker filter"

    def execute(self, context):
        _set_speaker_filter(context.scene, context, "")
        return {'FINISHED'}


class MyVoiceList_FilterScene(bpy.types.Operator):
    bl_idname = "witcher.quick_voice_filter_scene"
    bl_label = "Filter Scene"
    bl_description = "Filter list to only lines used by this scene"

    scene_path: StringProperty(default="")
    clear_other_filters: BoolProperty(default=True)

    @classmethod
    def description(cls, context, properties):
        scene_path = str(getattr(properties, "scene_path", "") or "")
        return f"Show dialogue lines used by {scene_path}" if scene_path else cls.bl_description

    def execute(self, context):
        scene_path = str(self.scene_path or "").strip()
        if not scene_path:
            self.report({'WARNING'}, "No scene path to filter")
            return {'CANCELLED'}
        if self.clear_other_filters:
            if hasattr(context.scene, "witcher_voice_search_text"):
                context.scene.witcher_voice_search_text = ""
            if hasattr(context.scene, "witcher_voice_speaker_filter"):
                context.scene.witcher_voice_speaker_filter = ""
        _set_scene_filter(context.scene, context, scene_path)
        self.report({'INFO'}, f"Filtered scene: {scene_path}")
        return {'FINISHED'}


class MyVoiceList_ClearSceneFilter(bpy.types.Operator):
    bl_idname = "witcher.quick_voice_clear_scene"
    bl_label = "Clear Scene Filter"
    bl_description = "Remove the current scene filter"

    def execute(self, context):
        _set_scene_filter(context.scene, context, "")
        return {'FINISHED'}


class VoicePinnedSpeaker(bpy.types.PropertyGroup):
    name: StringProperty(default="")

class MyVoiceList_PinSpeaker(bpy.types.Operator):
    bl_idname = "witcher.quick_voice_pin_speaker"
    bl_label = "Pin Speaker"
    bl_description = "Add this speaker to pinned filters"

    speaker: StringProperty(default="")

    def execute(self, context):
        scene = context.scene
        speaker = self.speaker or _get_selected_speaker(context)
        if not speaker:
            self.report({'WARNING'}, "No speaker selected to pin")
            return {'CANCELLED'}
        if _is_pinned(scene, speaker):
            return {'FINISHED'}
        pin = scene.witcher_voice_pinned_speakers.add()
        pin.name = speaker
        return {'FINISHED'}

class MyVoiceList_UnpinSpeaker(bpy.types.Operator):
    bl_idname = "witcher.quick_voice_unpin_speaker"
    bl_label = "Unpin Speaker"
    bl_description = "Remove this speaker from pinned filters"

    speaker: StringProperty(default="")

    def execute(self, context):
        scene = context.scene
        speaker = self.speaker or _get_selected_speaker(context)
        if not speaker:
            self.report({'WARNING'}, "No speaker selected to unpin")
            return {'CANCELLED'}
        for idx, pin in enumerate(scene.witcher_voice_pinned_speakers):
            if pin.name == speaker:
                scene.witcher_voice_pinned_speakers.remove(idx)
                break
        return {'FINISHED'}


classes = (
        VoicePinnedSpeaker,
        MyVoiceListItem,
        VoiceAssociatedPathItem,
        MyVoiceListItem_Expand,
        MyVoiceListItem_Debug,
        MyVoiceList_ClearSceneDialogueAudio,
        MyVoiceList_Page,
        MyVoiceListItem_Copy,
        MyVoiceList_ImportSpeakerEntity,
        MyVoiceList_CopyAssociatedPath,
        MyVoiceList_OpenAssociatedPath,
        MyVoiceList_ImportAssociatedEntity,
        MyVoiceList_ImportAssociatedScene,
        MyVoiceList_ToggleVoiceTagEntities,
        MyVoiceList_ClearFilter,
        MyVoiceList_BackSelect,
        MyVoiceList_FilterSpeaker,
        MyVoiceList_ClearSpeaker,
        MyVoiceList_FilterScene,
        MyVoiceList_ClearSceneFilter,
        MyVoiceList_PinSpeaker,
        MyVoiceList_UnpinSpeaker,
        MYVOICELISTITEM_UL_basic,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    # Scene.witcher_voice_nodes has been removed. The blender file was saving it!
    bpy.types.Scene.witcher_voice_game = EnumProperty(
        name="Game",
        description="Choose which game's dialogue data to browse",
        items=(
            (VOICE_GAME_W3, "Witcher 3", "Browse Witcher 3 speech and strings"),
            (VOICE_GAME_W2, "Witcher 2", "Browse Witcher 2 speech and strings"),
        ),
        default=VOICE_GAME_W3,
        update=_on_voice_game_update,
    )
    bpy.types.Scene.witcher_voice_list = bpy.props.CollectionProperty(
        type=MyVoiceListItem,
        options={'SKIP_SAVE'},
    )
    bpy.types.Scene.witcher_voice_list_index = IntProperty(
        options={'SKIP_SAVE'},
        default=-1,
        update=_on_voice_list_index_update,
    )
    bpy.types.Scene.witcher_voice_associated_paths = bpy.props.CollectionProperty(
        type=VoiceAssociatedPathItem,
        options={'SKIP_SAVE'},
    )
    bpy.types.Scene.witcher_voice_associated_paths_key = StringProperty(
        default="",
        options={'SKIP_SAVE'},
    )
    bpy.types.Scene.witcher_voice_selected_id = StringProperty(
        default="",
        options={'SKIP_SAVE'},
    )
    bpy.types.Scene.witcher_voice_previous_selected_id = StringProperty(
        default="",
        options={'SKIP_SAVE'},
    )
    bpy.types.Scene.witcher_voice_filter_anchor_id = StringProperty(
        default="",
        options={'SKIP_SAVE'},
    )
    bpy.types.Scene.witcher_voice_pinned_speakers = bpy.props.CollectionProperty(type=VoicePinnedSpeaker)
    bpy.types.Scene.witcher_voice_search_text = StringProperty(
        name="Search",
        default="",
        description="Search all dialogue text. Use @NAME or speaker:NAME to filter",
        update=_on_voice_search_update
    )
    bpy.types.Scene.witcher_voice_page_size = IntProperty(
        name="Rows",
        default=VOICE_PAGE_SIZE_DEFAULT,
        min=VOICE_PAGE_SIZE_MIN,
        max=VOICE_PAGE_SIZE_MAX,
        options={'SKIP_SAVE'},
        update=_on_voice_page_size_update,
    )
    bpy.types.Scene.witcher_voice_page_index = IntProperty(
        name="Page Index",
        default=0,
        options={'SKIP_SAVE'},
    )
    bpy.types.Scene.witcher_voice_page_number = IntProperty(
        name="Page",
        default=1,
        min=1,
        soft_min=1,
        options={'SKIP_SAVE'},
        update=_on_voice_page_number_update,
    )
    bpy.types.Scene.witcher_voice_speaker_filter = StringProperty(
        name="Speaker Filter",
        default="",
        options={'SKIP_SAVE'},
    )
    bpy.types.Scene.witcher_voice_scene_filter = StringProperty(
        name="Scene Filter",
        default="",
        description="Repo path of the .w2scene used to filter dialogue lines",
        options={'SKIP_SAVE'},
        update=_on_voice_scene_filter_update,
    )
    bpy.types.Scene.witcher_voice_show_details = BoolProperty(
        name="Show IDs/duration",
        default=True,
        description="Show IDs and duration in the dialogue list"
    )
    bpy.types.Scene.witcher_voice_replace_audio = BoolProperty(
        name="Replace audio",
        default=False,
        description="Replace existing sound strips instead of adding new channels"
    )
    bpy.types.Scene.witcher_voice_recreate_phonemes = BoolProperty(
        name="Recreate Phonemes",
        default=False,
        description="Solve phoneme curves from imported lipsync instead of using raw face morph curves"
    )
    bpy.types.Scene.witcher_voice_phoneme_accuracy = bpy.props.FloatProperty(
        name="Accuracy",
        default=0.5,
        min=0.0,
        max=1.0,
        description=(
            "How closely phonemes reproduce the original morph shapes. "
            "Low = clean single phonemes, High = multiple simultaneous phonemes for closer match"
        ),
    )
    bpy.types.Scene.witcher_voice_load_on_select = BoolProperty(
        name="Load on Select",
        default=False,
        description="Automatically load the voice line and lipsync whenever you highlight a new entry in the list"
    )
    bpy.types.Scene.witcher_voice_show_all_voicetag_entities = BoolProperty(
        name="Show VoiceTag Entities",
        default=False,
        options={'SKIP_SAVE'},
        description="Show all templates associated with the selected line's voice tag",
    )


def unregister():
    for prop_name in (
        "witcher_voice_game",
        VOICE_LIST_INDEX_PROP,
        VOICE_LIST_PROP,
        "witcher_voice_associated_paths",
        "witcher_voice_associated_paths_key",
        "witcher_voice_selected_id",
        "witcher_voice_previous_selected_id",
        "witcher_voice_filter_anchor_id",
        "witcher_voice_pinned_speakers",
        "witcher_voice_search_text",
        "witcher_voice_page_size",
        "witcher_voice_page_index",
        "witcher_voice_page_number",
        "witcher_voice_speaker_filter",
        "witcher_voice_scene_filter",
        "witcher_voice_show_details",
        "witcher_voice_replace_audio",
        "witcher_voice_recreate_phonemes",
        "witcher_voice_phoneme_accuracy",
        "witcher_voice_load_on_select",
        "witcher_voice_show_all_voicetag_entities",
    ):
        if hasattr(bpy.types.Scene, prop_name):
            delattr(bpy.types.Scene, prop_name)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
