import logging
import os
import struct

from .CR2W_types import getCR2W
from .common_blender import repo_file, win_safe_path
from .dc_skeleton import create_CMimicFace, load_bin_skeleton


log = logging.getLogger(__name__)

W2_MIMIC_FLOATTRACKS_RIG = r"characters\templates\mimics\floattracks.w2rig"


def is_w2_cr2w_version_file(file_name):
    try:
        with open(file_name, "rb") as f:
            if f.read(4) != b"CR2W":
                return False
            version = struct.unpack("<I", f.read(4))[0]
            return version <= 115
    except Exception:
        return False


def is_w2_cutscene_file(file_name):
    return str(file_name or "").lower().endswith(".w2cutscene")


def load_w2_base_skeleton(rig_path):
    if not rig_path:
        return None
    with open(rig_path, "rb") as f:
        the_file = getCR2W(f)
    if rig_path.endswith(".w3fac"):
        mimic_face = create_CMimicFace(the_file)
        return mimic_face.floatTrackSkeleton
    if rig_path.endswith(".w2rig"):
        # W2 rigs can store skeleton/track names in embedded Havok data. The
        # generic CSkeleton path can miss float-track names and shift channels.
        return load_bin_skeleton(rig_path)
    log.error("Error loading rig, check path and extension.")
    return None


def _load_w2_skeleton_name_sets(rig_path=None):
    if not rig_path:
        return None, None
    try:
        rig = load_w2_base_skeleton(rig_path)
    except Exception as exc:
        log.warning("Failed to load fallback rig for W2 bone mapping: %s", exc)
        return None, None
    names = getattr(rig, "names", None)
    tracks = getattr(rig, "tracks", None)
    return (names or None), (tracks or None)


def _resolve_default_w2_floattrack_rig(source_file=None):
    candidates = []
    if source_file:
        try:
            from ..source_game_paths import resolve_w2_repo_file_from_source

            resolved = resolve_w2_repo_file_from_source(
                W2_MIMIC_FLOATTRACKS_RIG,
                source_file,
                version=115,
            )
            if resolved:
                candidates.append(resolved)
        except Exception:
            pass

    try:
        candidates.append(repo_file(W2_MIMIC_FLOATTRACKS_RIG, version=115))
    except Exception:
        candidates.append(W2_MIMIC_FLOATTRACKS_RIG)

    seen = set()
    for candidate in candidates:
        if not candidate:
            continue
        key = os.path.normcase(os.path.normpath(str(candidate)))
        if key in seen:
            continue
        seen.add(key)
        if os.path.exists(win_safe_path(candidate)):
            return candidate
    return None


def get_fallback_w2_skeleton_names(rig_path=None, source_file=None):
    names, tracks = _load_w2_skeleton_name_sets(rig_path)
    if tracks:
        return names, tracks

    # Mimic/lipsync animsets usually do not embed the Havok skeleton, and
    # entity face rigs are not always the float-track name rig. Use the stock
    # float-track skeleton as the deterministic fallback.
    floattrack_rig = _resolve_default_w2_floattrack_rig(source_file)
    default_names, default_tracks = _load_w2_skeleton_name_sets(floattrack_rig)
    return (names or default_names), (tracks or default_tracks)


def get_fallback_w2_bone_names(rig_path=None):
    names, _tracks = get_fallback_w2_skeleton_names(rig_path)
    return names


def apply_decoded_animation_entry(entry, decoded):
    if not entry or not entry.animation or not decoded or not getattr(decoded, "buffer", None):
        return

    anim = entry.animation
    anim.animBuffer = decoded.buffer
    if decoded.duration > 0.0:
        anim.duration = decoded.duration
        anim.animBuffer.duration = decoded.duration

    if decoded.num_frames > 0:
        anim.animBuffer.numFrames = decoded.num_frames

    if anim.animBuffer.dt <= 0.0 and anim.duration > 0.0 and anim.animBuffer.numFrames > 1:
        anim.animBuffer.dt = anim.duration / float(anim.animBuffer.numFrames - 1)

    if anim.animBuffer.dt > 0.0:
        anim.framesPerSecond = 1.0 / anim.animBuffer.dt
    elif anim.duration > 0.0 and anim.animBuffer.numFrames > 1:
        anim.framesPerSecond = float(anim.animBuffer.numFrames - 1) / anim.duration
