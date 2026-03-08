import logging
from pathlib import Path
from ..importers import import_anims
log = logging.getLogger(__name__)
from .. import get_uncook_path, get_W3_VOICE_PATH, get_W3_OGG_PATH, get_vgmstream_path, get_all_addon_prefs
from ..extension_paths import get_cache_root, get_dev_override, get_dev_override_list
from ..CR2W.witcher_cache.Speech import LoadSpeechManager
from ..CR2W.witcher_cache.Speech.W3Speech import SpeechEntry
from ..CR2W.witcher_cache.W3Strings import LoadStringsManager
from . import phoneme_helper
from .ui_morphs import get_face_meshs

import csv
import os
import bpy
import math
import subprocess
import time
import numpy as np

from bpy.props import (
    IntProperty,
    BoolProperty,
    StringProperty,
)

VOICE_LIST_PROP = "witcher_voice_list"
VOICE_LIST_INDEX_PROP = "witcher_voice_list_index"

_voice_node_cache = []   # list[dict] — the full 64k voice line dataset
_voice_cache_loaded = False
_voice_filtered_indices = []
_VOICE_LIST_DEFERRED = False

def _voice_cache_path():
    """Return the writable user-cache path for the voice cache JSON file."""
    cache_dir = Path(get_cache_root(create=True)) / "Voice"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return str(cache_dir / "voice_cache.json")

def _save_voice_cache():
    """Write _voice_node_cache to disk as JSON."""
    global _voice_node_cache
    path = _voice_cache_path()
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({
                'version': 1,
                'count': len(_voice_node_cache),
                'nodes': _voice_node_cache,
            }, f, ensure_ascii=False)
        log.info("Voice cache saved: %d items → %s", len(_voice_node_cache), path)
    except Exception as exc:
        log.error("Failed to save voice cache: %s", exc)

def _load_voice_cache():
    """Load _voice_node_cache from the JSON file on disk. Returns True if loaded."""
    global _voice_node_cache, _voice_cache_loaded, _voice_filtered_indices
    path = _voice_cache_path()
    if not os.path.isfile(path):
        return False
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get('nodes'), list):
            _voice_node_cache = data['nodes']
            _voice_cache_loaded = True
            _voice_filtered_indices = list(range(len(_voice_node_cache)))
            _refresh_speaker_stats(_voice_node_cache)
            log.info("Voice cache loaded: %d items from %s", len(_voice_node_cache), path)
            return True
    except Exception as exc:
        log.error("Failed to load voice cache: %s", exc)
    return False

def _cache_is_stale():
    """Check if the cached data is stale compared to the speech manager."""
    try:
        speech_manager = LoadSpeechManager()
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

def ensure_voice_cache():
    """Load the voice cache from disk if not already loaded. Does NOT rebuild."""
    global _voice_cache_loaded
    if _voice_cache_loaded and _voice_node_cache:
        return
    _load_voice_cache()


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
    ensure_voice_cache()
    items = getattr(scene, VOICE_LIST_PROP, None)
    if items is None or len(items) > 0:
        return
    if _voice_node_cache:
        _schedule_deferred_voice_filter()

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


def _apply_phoneme_action(armature, pose_bone, phoneme_list, frames, phoneme_values, action_name, track_name="voice_import_phoneme"):
    if armature.animation_data is None:
        armature.animation_data_create()
    armature.animation_data.use_nla = True

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
        fcurve = action.fcurves.new(data_path=data_path)
        fcurve.keyframe_points.add(len(pts))
        for ki, (fr, val) in enumerate(pts):
            fcurve.keyframe_points[ki].co = (fr, val)
            fcurve.keyframe_points[ki].interpolation = 'LINEAR'

    track = armature.animation_data.nla_tracks.get(track_name)
    if track is None:
        track = armature.animation_data.nla_tracks.new()
        track.name = track_name
    else:
        for strip in list(track.strips):
            track.strips.remove(strip)

    if frames:
        start_frame = frames[0]
        end_frame = frames[-1] + 1
        strip = track.strips.new(action.name, int(start_frame), action)
        strip.frame_start = start_frame
        strip.frame_end = end_frame
        strip.blend_type = 'COMBINE'

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
        for track in list(shape_keys.animation_data.nla_tracks):
            if track.name == track_name:
                shape_keys.animation_data.nla_tracks.remove(track)
    if armature and armature.animation_data:
        for track in list(armature.animation_data.nla_tracks):
            if track.name == track_name:
                armature.animation_data.nla_tracks.remove(track)

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
                     display_compact="", search_blob=""):
    """Create a voice node dict (replaces the old MyVoiceListNode PropertyGroup)."""
    return {
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
    }

class MyVoiceListItem(bpy.types.PropertyGroup):
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
    """Parse raw search text into a list of match-token dicts.

    Each token dict has:
      'type':  'and' | 'not' | 'phrase' | 'id' | 'or'
      'terms': list[str]   — one str for 'and'/'not'/'phrase'/'id', N for 'or'

    Also returns the extracted speaker filter string (may be "").
    """
    if not raw_text:
        return [], ""

    tokens = []
    speaker_filter = ""
    pos = 0
    text = raw_text.strip()
    n = len(text)

    while pos < n:
        # skip whitespace
        while pos < n and text[pos] == ' ':
            pos += 1
        if pos >= n:
            break

        ch = text[pos]

        # --- quoted phrase ---
        if ch == '"':
            end = text.find('"', pos + 1)
            if end == -1:
                phrase = text[pos + 1:].strip().lower()
                pos = n
            else:
                phrase = text[pos + 1:end].strip().lower()
                pos = end + 1
            if phrase:
                tokens.append({'type': 'phrase', 'terms': [phrase]})
            continue

        # --- find end of token (space-delimited) ---
        end = pos
        while end < n and text[end] != ' ':
            end += 1
        raw = text[pos:end]
        pos = end

        lower = raw.lower()

        # --- speaker prefix: speaker:NAME or @NAME ---
        if lower.startswith('speaker:'):
            val = raw[8:].strip(' [](){}"').upper()
            if val:
                speaker_filter = val
            continue
        if raw.startswith('@'):
            val = raw[1:].strip(' [](){}"').upper()
            if val:
                speaker_filter = val
            continue
        # old [NAME] bracket syntax
        if raw.startswith('[') and raw.endswith(']') and len(raw) > 2:
            val = raw[1:-1].strip().upper()
            if val and not val.isdigit():
                speaker_filter = val
                continue

        # --- id: prefix ---
        if lower.startswith('id:'):
            val = raw[3:].strip().lower()
            if val:
                tokens.append({'type': 'id', 'terms': [val]})
            continue

        # --- negation: -term ---
        if raw.startswith('-') and len(raw) > 1:
            val = lower[1:]
            if val:
                tokens.append({'type': 'not', 'terms': [val]})
            continue

        # --- OR alternatives: word1|word2|word3 ---
        if '|' in raw:
            parts = [p.strip().lower() for p in raw.split('|') if p.strip()]
            if parts:
                tokens.append({'type': 'or', 'terms': parts})
            continue

        # --- plain AND term ---
        if lower:
            tokens.append({'type': 'and', 'terms': [lower]})

    return tokens, speaker_filter


def _matches_voice_filter_fast(blob, speaker, search_tokens, speaker_filter):
    """Return True if this voice node passes all search filters.

    blob           — pre-lowercased search blob string
    speaker        — uppercased speaker string from node
    search_tokens  — list of dicts from _parse_search_tokens
    speaker_filter — uppercased speaker name or '' to disable
    """
    # Speaker gate (fast exit)
    if speaker_filter and speaker != speaker_filter:
        return False

    for tok in search_tokens:
        ttype = tok['type']
        terms = tok['terms']
        if ttype == 'and' or ttype == 'phrase':
            if terms[0] not in blob:
                return False
        elif ttype == 'not':
            if terms[0] in blob:
                return False
        elif ttype == 'id':
            # blob starts with voice_id — do prefix check
            if not blob.startswith(terms[0]):
                return False
        elif ttype == 'or':
            if not any(t in blob for t in terms):
                return False
    return True


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
    try:
        size = int(value)
    except Exception:
        size = VOICE_PAGE_SIZE_DEFAULT
    return max(VOICE_PAGE_SIZE_MIN, min(VOICE_PAGE_SIZE_MAX, size))


def get_voice_filtered_count():
    return len(_voice_filtered_indices)


def get_voice_browser_stats(scene):
    ensure_voice_cache()
    total = len(_voice_node_cache)
    filtered = len(_voice_filtered_indices)
    page_size = _clamp_voice_page_size(getattr(scene, "witcher_voice_page_size", VOICE_PAGE_SIZE_DEFAULT))
    total_pages = max(1, int(math.ceil(filtered / page_size))) if filtered else 1
    page_index = int(getattr(scene, "witcher_voice_page_index", 0) or 0)
    page_index = max(0, min(page_index, total_pages - 1))
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


def _get_selected_voice_id(scene):
    if 0 <= scene.witcher_voice_list_index < len(scene.witcher_voice_list):
        return scene.witcher_voice_list[scene.witcher_voice_list_index].voiceLineId
    return str(getattr(scene, "witcher_voice_selected_id", "") or "")


def _set_selected_voice_id(scene, voice_id):
    if hasattr(scene, "witcher_voice_selected_id"):
        scene.witcher_voice_selected_id = voice_id or ""


def _set_list_item_from_node(item, node):
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


def _node_display_text(scene, node):
    show_details = getattr(scene, "witcher_voice_show_details", True)
    if show_details:
        return node.get('display_full') or node.get('name', '')
    return node.get('display_compact') or node.get('name', '')


def _refresh_voice_page(scene, selected_id=None):
    if not hasattr(scene, "witcher_voice_list"):
        return

    page_size = _clamp_voice_page_size(getattr(scene, "witcher_voice_page_size", VOICE_PAGE_SIZE_DEFAULT))
    if getattr(scene, "witcher_voice_page_size", page_size) != page_size:
        scene.witcher_voice_page_size = page_size

    filtered_total = len(_voice_filtered_indices)
    total_pages = max(1, int(math.ceil(filtered_total / page_size))) if filtered_total else 1
    page_index = int(getattr(scene, "witcher_voice_page_index", 0) or 0)
    page_index = max(0, min(page_index, total_pages - 1))
    scene.witcher_voice_page_index = page_index

    page_start = page_index * page_size
    page_end = min(page_start + page_size, filtered_total)
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

def _parse_search_text(text):
    """Legacy helper — extracts a cleaned text and speaker tag for backward compat.
    New code uses _parse_search_tokens directly."""
    _tokens, speaker = _parse_search_tokens(text)
    clean_parts = []
    for tok in _tokens:
        if tok['type'] in ('and', 'phrase', 'id'):
            clean_parts.append(tok['terms'][0])
        elif tok['type'] == 'or':
            clean_parts.append('|'.join(tok['terms']))
    return ' '.join(clean_parts), speaker

def _strip_speaker_tags(text):
    clean_text, _ = _parse_search_text(text)
    return clean_text

def _get_selected_speaker(context):
    scene = context.scene
    if 0 <= scene.witcher_voice_list_index < len(scene.witcher_voice_list):
        return scene.witcher_voice_list[scene.witcher_voice_list_index].speaker
    return ""

def _get_next_sound_channel(scene):
    if not scene.sequence_editor:
        return 1
    channels = [
        strip.channel for strip in scene.sequence_editor.sequences
        if strip.type == 'SOUND'
    ]
    return max(channels) + 1 if channels else 1

def _is_pinned(scene, speaker):
    if not speaker:
        return False
    for pin in scene.witcher_voice_pinned_speakers:
        if pin.name == speaker:
            return True
    return False

def _set_speaker_filter(scene, context, speaker):
    scene.witcher_voice_speaker_filter = speaker.upper() if speaker else ""
    if hasattr(scene, "witcher_voice_page_index"):
        scene.witcher_voice_page_index = 0
    if scene.witcher_voice_search_text:
        scene.witcher_voice_search_text = _strip_speaker_tags(scene.witcher_voice_search_text)
    _apply_voice_filter(context)

def _get_effective_speaker(scene):
    _, speaker_from_search = _parse_search_text(scene.witcher_voice_search_text.strip())
    if speaker_from_search:
        return speaker_from_search
    return scene.witcher_voice_speaker_filter.strip().upper()

def _apply_voice_filter(context):
    global _voice_filtered_indices, _voice_filter_pending_final
    scene = context.scene
    if not hasattr(scene, "witcher_voice_list"):
        return

    ensure_voice_cache()
    if not _voice_node_cache:
        _voice_filtered_indices = []
        if hasattr(scene, "witcher_voice_list"):
            scene.witcher_voice_list.clear()
        return

    raw_search_text = scene.witcher_voice_search_text.strip()
    search_tokens, speaker_from_search = _parse_search_tokens(raw_search_text)
    speaker_filter = speaker_from_search or scene.witcher_voice_speaker_filter.strip().upper()

    selected_id = _get_selected_voice_id(scene)

    # Fast path: no filters at all → all indices
    if not search_tokens and not speaker_filter:
        _voice_filtered_indices = list(range(len(_voice_node_cache)))
        _voice_filter_pending_final = False
        _refresh_voice_page(scene, selected_id=selected_id)
        return

    # Build filtered list with the tight inner loop
    result = []
    cache = _voice_node_cache
    n = len(cache)
    for idx in range(n):
        node = cache[idx]
        blob = node.get('search_blob', '') or ''
        speaker = node.get('speaker', '') or ''
        if _matches_voice_filter_fast(blob, speaker, search_tokens, speaker_filter):
            result.append(idx)

    _voice_filtered_indices = result
    _voice_filter_pending_final = False
    _refresh_voice_page(scene, selected_id=selected_id)

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
        _refresh_voice_page(scene, selected_id=_get_selected_voice_id(scene))
    elif _voice_node_cache:
        _apply_voice_filter(context)


def _on_voice_list_index_update(self, context):
    if context is None or getattr(context, "scene", None) is None:
        return
    scene = context.scene
    _set_selected_voice_id(scene, _get_selected_voice_id(scene))
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
        try:
            bpy.ops.witcher.load_face_morphs()
        except Exception as exc:
            log.warning("Auto face morph load failed: %s", exc)
    load_voice_and_lipsync(item.voiceLineId)


def has_invalid_surrogates(s):
    # Surrogate range: 0xD800 - 0xDFFF
    for char in s:
        if 0xD800 <= ord(char) <= 0xDFFF:
            return True
    return False

import json

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

def SetupNodeData(do_reload_strings = False):
    global _voice_node_cache, _voice_cache_loaded, _voice_filtered_indices
    speech_manager = LoadSpeechManager()
    strings_manager = LoadStringsManager(do_reload = do_reload_strings)
    voice_data = _load_voice_name_map()
    voiceList = VoiceLineResourceManager().Get()
    
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
        character_name = voice_data.get(voice_id_str)
        if not character_name:
            character_name = voiceList.SpeakerById.get(voice_id_str, "")
        
        character_name = char_dict.get(character_name) if character_name in char_dict else character_name
        
        speaker = character_name.upper() if character_name else 'UNKN'
        duration = round(item.duration, 2)
        display_full = "{} [{}] {} |{}".format(voice_id_str, speaker, text, duration)
        display_compact = "[{}] {}".format(speaker, text)
        # search_blob: voice_id + speaker + full dialogue text + speaker-lower (for partial name search)
        # Keep lower-cased so all matching is case-insensitive substring search
        speaker_lower = speaker.lower()
        search_blob = "{} {} {} {}".format(voice_id_str, speaker_lower, text.lower(), str(duration))
        
        node = _make_voice_node(
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
    _voice_cache_loaded = True
    
    # Persist to addon-managed JSON file
    _save_voice_cache()
        

def NewListItem( voiceList, node):
    item = voiceList.add()
    # node may be a dict (from _voice_node_cache)
    if isinstance(node, dict):
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
    else:
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
    return item


def SetupListFromNodeData():
    ensure_voice_cache()
    _refresh_speaker_stats(_voice_node_cache)
    if bpy.context and getattr(bpy.context, "scene", None):
        try:
            bpy.context.scene.witcher_voice_page_index = 0
        except Exception:
            pass
    _apply_voice_filter(bpy.context)

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
        
        ensure_voice_cache()
        
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
def load_voice_and_lipsync(voiceLineId, actor = None, context = None, at_frame = 0, recreate_phonemes = None):
    unpadded_line_id = ''+voiceLineId
    if context == None:
        context = bpy.context
    if recreate_phonemes is None:
        recreate_phonemes = getattr(context.scene, "witcher_voice_recreate_phonemes", False)
    namelen = len(voiceLineId)
    if namelen != 10:
        zeros = "0000000000"
        num_of_zeros = 10 - namelen
        voiceLineId = zeros[:num_of_zeros] + voiceLineId
    sound_directory_to_check: Path = Path(get_W3_OGG_PATH(context))
    cr2w_directory_to_check: Path =  Path(get_W3_VOICE_PATH(context))
    
    soundPath: Path = sound_directory_to_check / f"{voiceLineId}.ogg"
    soundPath_wav: Path = sound_directory_to_check / f"{voiceLineId}.wav"
    cr2wPath: Path = cr2w_directory_to_check / f"{voiceLineId}.cr2w"
    wemPath: Path = cr2w_directory_to_check / f"{voiceLineId}.wem"
    
    
    ##? RADISH CHECKING
    if not cr2wPath.is_file():
        for dir in radish_dirs:
            dir = Path(dir) / "speech/speech.en.wem"
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
        speech_manager = LoadSpeechManager()
        item:SpeechEntry = speech_manager.find_item_by_hash(unpadded_line_id)[0]
        item.extract_to_file(str(item.id))

    if cr2wPath.is_file():
        log.info('Importing Lipsync')
        import_anims.import_lipsync(context, str(cr2wPath), use_NLA=True, NLA_track="voice_import", override_select=actor, at_frame=at_frame)
        # Ensure the newly created NLA track evaluates during morph sampling.
        _actor_arm = actor if (actor and getattr(actor, 'type', None) == 'ARMATURE') else getattr(actor, 'parent', None)
        if _actor_arm and getattr(_actor_arm, 'type', None) == 'ARMATURE':
            if _actor_arm.animation_data:
                _actor_arm.animation_data.use_nla = True
        if recreate_phonemes:
            armature = None
            if actor and isinstance(actor, bpy.types.Object):
                if actor.type == 'ARMATURE':
                    armature = actor
                elif actor.parent and actor.parent.type == 'ARMATURE':
                    armature = actor.parent
            if armature is None:
                armature = _get_active_armature(context)
            if armature is None:
                raise RuntimeError(
                    "Recreate Phonemes failed: no character armature found. "
                    "Set a character target or select an armature."
                )
            # Will raise RuntimeError with a descriptive message on failure.
            _recreate_phonemes_from_lipsync(context, armature, voiceLineId, track_name="voice_import")

    if not soundPath.is_file() and not soundPath_wav.is_file():
        vgmstream_path = get_vgmstream_path(context)
        output_folder = get_W3_OGG_PATH(context)
        if wemPath.is_file() and os.path.isfile(vgmstream_path):
            if not output_folder:
                output_folder = bpy.app.tempdir

            output_wav = os.path.join(output_folder, os.path.basename(str(wemPath)).replace('.wem', '.wav'))
            command = [vgmstream_path, "-o", output_wav, str(wemPath)]

            try:
                subprocess.run(command, check=True)
                # Here you might want to add the WAV to Blender's sequencer
            except subprocess.CalledProcessError as e:
                log.error("vgmstream conversion failed: %s", e)
        
    if soundPath.is_file() or soundPath_wav.is_file():
        if not soundPath.is_file() and soundPath_wav.is_file():
            soundPath = soundPath_wav
        log.info('Importing Sound')
        scene = context.scene 

        if not scene.sequence_editor:
            scene.sequence_editor_create()

        if getattr(scene, "witcher_voice_replace_audio", False):
            sound_strips = [strip for strip in scene.sequence_editor.sequences if strip.type == 'SOUND']
            for strip in sound_strips:
                scene.sequence_editor.sequences.remove(strip)

        # try:
        #     soundstrip = scene.sequence_editor.sequences.new_sound("voiceline", str(soundPath), 1, at_frame)
        # except Exception as e:
        channel = 1 if getattr(scene, "witcher_voice_replace_audio", False) else _get_next_sound_channel(scene)
        soundstrip = scene.sequence_editor.sequences.new_sound(
            soundPath.stem,
            str(soundPath),
            channel=channel,
            frame_start= math.ceil(at_frame)+1
        )
        soundstrip.frame_start = at_frame
        # Only extend frame_end, never shrink it
        strip_end = int(math.ceil(soundstrip.frame_final_end))
        if strip_end > scene.frame_end:
            scene.frame_end = strip_end

class MyVoiceListItem_Debug(bpy.types.Operator):
    bl_idname = "witcher.quick_voice_debug"
    bl_label = "Debug"
    
    @classmethod
    def description(cls, context, properties):
        if properties.action == "reset3":
            return "Rebuild the dialogue list from game data"
        if properties.action == "load":
            return "Load the selected line onto the character/armature"
        if properties.action == "clear":
            return "Clear the current dialogue list"
        return ""

    action: StringProperty(default="default")
    
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
                    try:
                        bpy.ops.witcher.load_face_morphs()
                    except Exception as exc:
                        log.warning("Auto face morph load failed: %s", exc)

                filename = item.voiceLineId
                load_voice_and_lipsync(filename)
                
        elif "reset3" == action:
            log.debug("=== Debug Reset ====")
            SetupNodeData(do_reload_strings = True)
            SetupListFromNodeData()
        elif "clear" == action:
            log.debug("=== Debug Clear ====")
            _voice_filtered_indices = []
            bpy.context.scene.witcher_voice_list.clear()
            scene.witcher_voice_list_index = -1
            if hasattr(scene, "witcher_voice_page_index"):
                scene.witcher_voice_page_index = 0
            _set_selected_voice_id(scene, "")
        else:
            log.warning("unknown debug action: %s", action)

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
        current = stats["page_index"]
        last = max(0, stats["total_pages"] - 1)
        if self.action == "first":
            target = 0
        elif self.action == "prev":
            target = max(0, current - 1)
        elif self.action == "next":
            target = min(last, current + 1)
        elif self.action == "last":
            target = last
        else:
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
            ensure_voice_cache()
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

class MYVOICELISTITEM_UL_basic(bpy.types.UIList):
    use_filter_show = False
    use_filter_sort_alpha = False

    def draw_item(
        self, context, layout, data, item, icon,
        active_data, active_propname, index
    ):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            display_text = _get_display_text(context.scene, item)
            layout.label(text=display_text)
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
    bl_description = "Clear search text and speaker filter"

    def execute(self, context):
        scene = context.scene
        scene.witcher_voice_search_text = ""
        scene.witcher_voice_speaker_filter = ""
        if hasattr(scene, "witcher_voice_page_index"):
            scene.witcher_voice_page_index = 0
        _apply_voice_filter(context)
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
        MyVoiceListItem_Expand,
        MyVoiceListItem_Debug,
        MyVoiceList_Page,
        MyVoiceListItem_Copy,
        MyVoiceList_ClearFilter,
        MyVoiceList_FilterSpeaker,
        MyVoiceList_ClearSpeaker,
        MyVoiceList_PinSpeaker,
        MyVoiceList_UnpinSpeaker,
        MYVOICELISTITEM_UL_basic,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    # Scene.witcher_voice_nodes has been removed. The blender file was saving it!
    bpy.types.Scene.witcher_voice_list = bpy.props.CollectionProperty(
        type=MyVoiceListItem,
        options={'SKIP_SAVE'},
    )
    bpy.types.Scene.witcher_voice_list_index = IntProperty(
        options={'SKIP_SAVE'},
        default=-1,
        update=_on_voice_list_index_update,
    )
    bpy.types.Scene.witcher_voice_selected_id = StringProperty(
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
    bpy.types.Scene.witcher_voice_speaker_filter = StringProperty(
        name="Speaker Filter",
        default="",
        options={'SKIP_SAVE'},
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


def unregister():
    for prop_name in (
        VOICE_LIST_INDEX_PROP,
        VOICE_LIST_PROP,
        "witcher_voice_selected_id",
        "witcher_voice_pinned_speakers",
        "witcher_voice_search_text",
        "witcher_voice_page_size",
        "witcher_voice_page_index",
        "witcher_voice_speaker_filter",
        "witcher_voice_show_details",
        "witcher_voice_replace_audio",
        "witcher_voice_recreate_phonemes",
        "witcher_voice_phoneme_accuracy",
        "witcher_voice_load_on_select",
    ):
        if hasattr(bpy.types.Scene, prop_name):
            delattr(bpy.types.Scene, prop_name)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
