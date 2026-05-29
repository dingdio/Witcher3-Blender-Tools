import logging
import math
import os
import re

from . import w3_types
from .CR2W_types import getCR2W
from .dc_w2_havok import apply_decoded_animation_entry, get_fallback_w2_skeleton_names
from .havok_parser import HavokPackfile
from .prop_utils import (
    read_array_string_prop as _read_array_string_prop,
    read_bool_prop as _read_bool_prop,
    read_cname_prop as _read_cname_prop,
    read_datetime_prop as _read_datetime_prop,
    read_enum_prop as _read_enum_prop,
    read_float_prop as _read_float_prop,
    read_handle_labels_prop as _read_handle_labels_prop,
    read_int_prop as _read_int_prop,
    read_prop_value as _read_prop_value,
    read_ptr_chunk_labels_prop as _read_ptr_chunk_labels_prop,
    read_single_handle_label_prop as _read_single_handle_label_prop,
    read_string_prop as _read_string_prop,
    read_taglist_prop as _read_taglist_prop,
)


log = logging.getLogger(__name__)

_W2_DEPOT_PATH_RE = re.compile(rb"[ -~]*?\.w2[a-z0-9]+")


def _read_cutscene_field_from_prop(field_name, prop, chunks):
    if prop is None:
        return None

    if field_name in {"animations", "actorsDef", "effects"}:
        return None
    if field_name in {"modifiers", "compressedPoses"}:
        return _read_ptr_chunk_labels_prop(prop, chunks)
    if field_name in {"point"}:
        return _read_taglist_prop(prop)
    if field_name in {"importFile", "lastLevelLoaded", "reverbName", "burnedAudioTrackName"}:
        return _read_string_prop(prop)
    if field_name in {"requiredSfxTag"}:
        return _read_cname_prop(prop)
    if field_name in {"importFileTimeStamp"}:
        return _read_datetime_prop(prop)
    if field_name in {"isValid", "blackscreenWhenLoading", "checkActorsPosition", "streamable",
                      "forceUncompressedImport", "overrideBitwiseCompressionSettingsOnImport"}:
        return _read_bool_prop(prop)
    if field_name in {"fadeBefore", "fadeAfter", "cameraBlendInTime", "cameraBlendOutTime"}:
        return _read_float_prop(prop)
    if field_name in {"entToHideTags", "usedInFiles", "resourcesToPreloadManuallyPaths", "banksDependency"}:
        return _read_array_string_prop(prop)
    if field_name in {"resourcesToPreloadManually"}:
        return _read_handle_labels_prop(prop, chunks)
    if field_name in {"extAnimEvents"}:
        return _read_prop_value(prop, chunks)
    if field_name in {"skeleton"}:
        return _read_single_handle_label_prop(prop, chunks)
    if field_name in {"Streaming option", "bitwiseCompressionPreset"}:
        return _read_enum_prop(prop)
    if field_name in {"Number of non-streamable bones"}:
        return _read_int_prop(prop)

    return _read_prop_value(prop, chunks)


def _read_w2_soft_dependency_paths(cf, raw_data):
    """Return the ordered depot paths from a W2 CR2W soft-dependency table."""
    try:
        tables = getattr(cf, "CR2WTable", None)
        if not tables or len(tables) <= 3:
            return []
        start = int(getattr(cf, "start", 0) or 0)
        beg = int(tables[3].offset) + start
        if beg <= 0 or beg >= len(raw_data):
            return []
        candidates = [int(t.offset) + start for t in tables]
        ends = [off for off in candidates if beg < off <= len(raw_data)]
        end = min(ends) if ends else len(raw_data)
        region = raw_data[beg:end]
    except Exception:
        log.debug("Failed to slice W2 soft-dependency table region", exc_info=True)
        return []

    paths = []
    for match in _W2_DEPOT_PATH_RE.finditer(region):
        try:
            text = match.group(0).decode("latin-1").strip()
        except Exception:
            continue
        text = text.replace("/", "\\").lstrip("\\")
        if text:
            paths.append(text)
    return paths


def _resolve_w2_actor_templates(actors, raw_actor_elements, soft_dep_paths):
    for actor, element in zip(actors, raw_actor_elements):
        template_path = ""
        try:
            template_prop = element.GetVariableByName("template")
            index_obj = getattr(template_prop, "Index", None)
            soft_index = int(getattr(index_obj, "Index", 0) or 0)
            if 1 <= soft_index <= len(soft_dep_paths):
                template_path = soft_dep_paths[soft_index - 1]
        except Exception:
            log.debug("Failed to resolve W2 cutscene actor template", exc_info=True)
        actor.template = template_path


def _read_w2_cutscene_anim_name(anim_chunk):
    name_prop = anim_chunk.GetVariableByName("name")
    if name_prop is None:
        return "unknown"
    if getattr(name_prop, "Index", None) is not None:
        return name_prop.Index.String
    try:
        return name_prop.ToString()
    except Exception:
        return "unknown"


def _valid_w2_anim_time_starts(anim_times):
    starts = []
    for value in anim_times or []:
        try:
            time_value = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(time_value) or time_value < 0.0:
            continue
        if not starts or abs(time_value - starts[-1]) > 1e-5:
            starts.append(time_value)
    return starts


def _w2_havok_info_frame_count(hk_info, duration=0.0, fps=30.0):
    if hk_info is not None:
        num_frames = int(getattr(hk_info, "num_frames", 0) or 0)
        if num_frames > 0:
            return num_frames
        duration = float(getattr(hk_info, "duration", 0.0) or duration or 0.0)
    if duration > 0.0 and fps > 0.0:
        return max(1, int(round(duration * fps)) + 1)
    return 1


def _w2_havok_info_duration(hk_info, fallback_duration=0.0, fps=30.0):
    duration = float(getattr(hk_info, "duration", 0.0) or 0.0) if hk_info is not None else 0.0
    if duration > 0.0:
        return duration
    fallback_duration = float(fallback_duration or 0.0)
    if fallback_duration > 0.0:
        return fallback_duration
    num_frames = _w2_havok_info_frame_count(hk_info, fps=fps)
    if num_frames > 1 and fps > 0.0:
        return float(num_frames - 1) / float(fps)
    return 0.0


def _w2_part_first_frames(part_infos, anim_time_starts=None, fps=30.0):
    part_infos = list(part_infos or [])
    anim_time_starts = list(anim_time_starts or [])
    if anim_time_starts and len(anim_time_starts) >= len(part_infos):
        return [max(0, int(round(float(time_value) * float(fps or 30.0)))) for time_value in anim_time_starts[:len(part_infos)]]

    first_frames = []
    cursor = 0
    for hk_info in part_infos:
        first_frames.append(cursor)
        frame_count = _w2_havok_info_frame_count(hk_info, fps=fps)
        cursor += max(0, frame_count - 1)
    return first_frames or [0]


def _w2_total_frame_count(part_infos, first_frames=None, fps=30.0):
    part_infos = list(part_infos or [])
    first_frames = list(first_frames or [])
    if not part_infos:
        return 0
    if not first_frames:
        first_frames = _w2_part_first_frames(part_infos, fps=fps)
    last_start = int(first_frames[-1] if first_frames else 0)
    last_frames = _w2_havok_info_frame_count(part_infos[-1], fps=fps)
    return max(1, last_start + max(1, last_frames))


def _w2_total_duration(part_infos, first_frames=None, fps=30.0):
    part_infos = list(part_infos or [])
    if not part_infos:
        return 0.0
    first_frames = list(first_frames or [])
    if first_frames and len(first_frames) == len(part_infos) and fps > 0.0:
        return (float(first_frames[-1]) / float(fps)) + _w2_havok_info_duration(part_infos[-1], fps=fps)
    return sum(_w2_havok_info_duration(hk_info, fps=fps) for hk_info in part_infos)


def _make_w2_cutscene_part_stub(hk_info, fps=30.0):
    duration = _w2_havok_info_duration(hk_info, fps=fps)
    num_frames = _w2_havok_info_frame_count(hk_info, duration=duration, fps=fps)
    dt = (duration / float(max(num_frames - 1, 1))) if duration > 0.0 and num_frames > 1 else (1.0 / float(fps or 30.0))
    return w3_types.CAnimationBufferBitwiseCompressed(
        [], [],
        duration=duration,
        numFrames=num_frames,
        dt=dt,
    )


def _w2_cutscene_part_count_from_anim_chunk(anim_chunk):
    anim_times = None
    for prop in getattr(anim_chunk, "PROPS", None) or []:
        if prop.theName == "animTimes" and getattr(prop, "value", None):
            anim_times = prop.value
            break
    return len(_valid_w2_anim_time_starts(anim_times))


def _infer_w2_cutscene_havok_part_count(havok_infos, anim_chunks):
    anim_count = len(anim_chunks or [])
    blob_count = len(havok_infos or [])
    if anim_count <= 0 or blob_count <= 0:
        return 1

    time_counts = [
        _w2_cutscene_part_count_from_anim_chunk(anim_chunk)
        for anim_chunk in anim_chunks or []
    ]
    time_counts = [count for count in time_counts if count > 0]
    if time_counts and len(set(time_counts)) == 1:
        part_count = time_counts[0]
        if part_count > 0 and blob_count == anim_count * part_count:
            return part_count

    if blob_count % anim_count == 0:
        return max(1, blob_count // anim_count)
    return 1


def _group_w2_cutscene_havok_infos(havok_infos, anim_count, part_count):
    havok_infos = list(havok_infos or [])
    anim_count = int(anim_count or 0)
    part_count = max(1, int(part_count or 1))
    groups = []
    for idx in range(anim_count):
        start = idx * part_count
        end = start + part_count
        groups.append(havok_infos[start:end])
    return groups


def _build_w2_cutscene_anim_entry(entry_chunk, anim_chunk, hk_infos, default_fps=30.0):
    name = _read_w2_cutscene_anim_name(anim_chunk)

    duration = 0.0
    frames_per_second = float(default_fps or 30.0)
    num_frames = 0
    anim_times = None
    has_motion = False
    for prop in getattr(anim_chunk, "PROPS", None) or []:
        if prop.theName == "duration":
            duration = prop.Value
        elif prop.theName == "framesPerSecond":
            frames_per_second = prop.Value
        elif prop.theName == "animTimes":
            if getattr(prop, "value", None):
                anim_times = prop.value
        elif prop.theName == "motionExtraction":
            has_motion = True

    if hk_infos is None:
        part_infos = []
    elif isinstance(hk_infos, (list, tuple)):
        part_infos = list(hk_infos)
    else:
        part_infos = [hk_infos]
    anim_time_starts = _valid_w2_anim_time_starts(anim_times)

    if part_infos:
        first_frames = _w2_part_first_frames(part_infos, anim_time_starts, frames_per_second)
        duration = _w2_total_duration(part_infos, first_frames, frames_per_second)
        num_frames = _w2_total_frame_count(part_infos, first_frames, frames_per_second)

    if duration == 0.0 and anim_times:
        for val in anim_times:
            if val and not math.isnan(val) and val > 0.0:
                duration = val
                break

    if num_frames == 0 and duration > 0.0 and frames_per_second > 0.0:
        num_frames = max(1, int(round(duration * frames_per_second)) + 1)
    if frames_per_second <= 0.0 and duration > 0.0 and num_frames > 1:
        frames_per_second = float(num_frames - 1) / duration

    if len(part_infos) > 1:
        first_frames = _w2_part_first_frames(part_infos, anim_time_starts, frames_per_second)
        stub_buffer = w3_types.CAnimationBufferMultipart(
            numFrames=num_frames,
            numBones=max((int(getattr(info, "num_transform_tracks", 0) or 0) for info in part_infos), default=0),
            numTracks=max((int(getattr(info, "num_float_tracks", 0) or 0) for info in part_infos), default=0),
            firstFrames=first_frames,
            parts=[_make_w2_cutscene_part_stub(info, frames_per_second) for info in part_infos],
        )
    else:
        stub_buffer = w3_types.CAnimationBufferBitwiseCompressed(
            [], [], duration=duration,
            numFrames=num_frames,
            dt=(duration / max(num_frames - 1, 1)) if num_frames > 1 else 0.0,
        )

    anim = w3_types.CSkeletalAnimation(
        name, duration, frames_per_second,
        animBuffer=stub_buffer,
        SkeletalAnimationType="SAT_Normal",
        AdditiveType=None,
        motionExtraction={"duration": duration, "frames": [], "deltaTimes": [], "flags": 0} if has_motion else None,
    )
    return w3_types.CSkeletalAnimationSetEntry(anim, [])


def _w2_entry_part_count(entry):
    buffer = getattr(getattr(entry, "animation", None), "animBuffer", None)
    parts = getattr(buffer, "parts", None)
    if parts:
        return max(1, len(parts))
    return 1


def _w2_entry_first_frames(entry):
    buffer = getattr(getattr(entry, "animation", None), "animBuffer", None)
    return list(getattr(buffer, "firstFrames", None) or [])


def _decoded_buffer_num_frames(buffer):
    num_frames = int(getattr(buffer, "numFrames", 0) or 0)
    if num_frames > 0:
        return num_frames
    bones = list(getattr(buffer, "bones", None) or [])
    tracks = list(getattr(buffer, "tracks", None) or [])
    counts = []
    for bone in bones:
        counts.extend([
            int(getattr(bone, "position_numFrames", 0) or len(getattr(bone, "positionFrames", []) or [])),
            int(getattr(bone, "rotation_numFrames", 0) or len(getattr(bone, "rotationFramesQuat", []) or [])),
            int(getattr(bone, "scale_numFrames", 0) or len(getattr(bone, "scaleFrames", []) or [])),
        ])
    for track in tracks:
        counts.append(int(getattr(track, "numFrames", 0) or len(getattr(track, "trackFrames", []) or [])))
    return max(counts, default=1)


def _decoded_buffer_duration(buffer, fps=30.0):
    duration = float(getattr(buffer, "duration", 0.0) or 0.0)
    if duration > 0.0:
        return duration
    dt = float(getattr(buffer, "dt", 0.0) or 0.0)
    num_frames = _decoded_buffer_num_frames(buffer)
    if dt > 0.0 and num_frames > 1:
        return dt * float(num_frames - 1)
    if fps > 0.0 and num_frames > 1:
        return float(num_frames - 1) / float(fps)
    return 0.0


def _apply_decoded_w2_cutscene_parts(entry, decoded_parts, first_frames=None):
    if not entry or not entry.animation:
        return

    decoded_parts = list(decoded_parts or [])
    first_frames = list(first_frames or [])
    valid_parts = []
    valid_first_frames = []
    for idx, decoded in enumerate(decoded_parts):
        buffer = getattr(decoded, "buffer", None)
        if buffer is None:
            continue
        valid_parts.append(buffer)
        if idx < len(first_frames):
            valid_first_frames.append(int(first_frames[idx]))

    if not valid_parts:
        return

    if len(valid_parts) == 1:
        decoded = next((part for part in decoded_parts if getattr(part, "buffer", None) is valid_parts[0]), None)
        apply_decoded_animation_entry(entry, decoded)
        return

    anim = entry.animation
    fps = float(getattr(anim, "framesPerSecond", 0.0) or 0.0)
    if fps <= 0.0:
        first_dt = float(getattr(valid_parts[0], "dt", 0.0) or 0.0)
        fps = (1.0 / first_dt) if first_dt > 0.0 else 30.0
    if len(valid_first_frames) != len(valid_parts):
        valid_first_frames = []
        cursor = 0
        for buffer in valid_parts:
            valid_first_frames.append(cursor)
            cursor += max(0, _decoded_buffer_num_frames(buffer) - 1)

    total_num_frames = max(
        1,
        int(valid_first_frames[-1]) + max(1, _decoded_buffer_num_frames(valid_parts[-1])),
    )
    total_duration = (float(valid_first_frames[-1]) / fps) + _decoded_buffer_duration(valid_parts[-1], fps=fps)
    if total_duration <= 0.0:
        total_duration = sum(_decoded_buffer_duration(buffer, fps=fps) for buffer in valid_parts)

    anim.animBuffer = w3_types.CAnimationBufferMultipart(
        numFrames=total_num_frames,
        numBones=max((len(getattr(buffer, "bones", None) or []) for buffer in valid_parts), default=0),
        numTracks=max((len(getattr(buffer, "tracks", None) or []) for buffer in valid_parts), default=0),
        firstFrames=valid_first_frames,
        parts=valid_parts,
    )
    anim.duration = total_duration
    anim.framesPerSecond = fps


def create_CCutscene_w2(file, raw_data):
    """Build a CCutsceneTemplate from a Witcher 2 (CR2W <=115) .w2cutscene."""
    chunks = file.CHUNKS.CHUNKS
    set_chunk = None
    for chunk in chunks:
        if chunk.name == "CCutsceneTemplate":
            set_chunk = chunk
            break
    if set_chunk is None:
        log.error("No CCutsceneTemplate chunk found in W2 cutscene")
        return w3_types.CCutsceneTemplate(animations=[], SCutsceneActorDefs=[])

    actors_def = set_chunk.GetVariableByName("actorsDef")
    actors = []
    raw_actor_elements = []
    if actors_def is not None and hasattr(actors_def, "More"):
        for actor in actors_def.More:
            try:
                actors.append(w3_types.SCutsceneActorDef(False, actor))
                raw_actor_elements.append(actor)
            except Exception:
                log.warning("Failed to parse W2 cutscene actor def", exc_info=True)

    soft_dep_paths = _read_w2_soft_dependency_paths(file, raw_data)
    _resolve_w2_actor_templates(actors, raw_actor_elements, soft_dep_paths)

    havok_infos = HavokPackfile.scan_animation_blobs(raw_data) or []

    set_animations = set_chunk.GetVariableByName("animations")
    anim_ptrs = []
    if set_animations is not None:
        if getattr(set_animations, "value", None):
            anim_ptrs = list(set_animations.value)
        elif getattr(set_animations, "Handles", None):
            anim_ptrs = [h.val for h in set_animations.Handles if getattr(h, "val", None)]

    anim_chunks = []
    for anim_ptr in anim_ptrs:
        try:
            entry_chunk = chunks[anim_ptr - 1]
            anim_prop = entry_chunk.GetVariableByName("animation")
            anim_idx = getattr(anim_prop, "Value", None)
            if not anim_idx and getattr(anim_prop, "Handles", None):
                anim_idx = anim_prop.Handles[0].val
            anim_chunks.append(chunks[anim_idx - 1])
        except Exception:
            anim_chunks.append(None)

    havok_part_count = _infer_w2_cutscene_havok_part_count(havok_infos, anim_chunks)
    havok_info_groups = _group_w2_cutscene_havok_infos(
        havok_infos,
        len(anim_ptrs),
        havok_part_count,
    )
    expected_blob_count = len(anim_ptrs) * havok_part_count
    if len(havok_infos) != expected_blob_count:
        log.warning(
            "W2 cutscene Havok blob count (%d) does not match animation entries (%d) x parts (%d)",
            len(havok_infos), len(anim_ptrs), havok_part_count,
        )

    animations = []
    for idx, anim_ptr in enumerate(anim_ptrs):
        try:
            entry_chunk = chunks[anim_ptr - 1]
            anim_chunk = anim_chunks[idx] if idx < len(anim_chunks) else None
            if anim_chunk is None:
                continue
            hk_infos = havok_info_groups[idx] if idx < len(havok_info_groups) else []
            entry = _build_w2_cutscene_anim_entry(entry_chunk, anim_chunk, hk_infos)
            animations.append(entry)
            log.info("%d %s", idx, entry.animation.name)
        except Exception:
            log.warning("Failed to read W2 cutscene animation entry %d", idx, exc_info=True)

    final_set = w3_types.CCutsceneTemplate(animations=animations, SCutsceneActorDefs=actors)
    final_set.animevents = []  # W2 events (CAnimEventSerializer) not parsed yet

    present_fields = {
        str(getattr(prop, "theName", "") or "").strip()
        for prop in getattr(set_chunk, "PROPS", None) or []
        if str(getattr(prop, "theName", "") or "").strip()
    }
    final_set.presentPropertyNames = present_fields
    final_set.presentTemplateProps = present_fields

    for _class_name, fields in getattr(w3_types, "CUTSCENE_CLASS_FIELD_SCHEMA", ()):
        for field_name, _default in fields:
            if field_name in {"animations", "actorsDef", "effects"}:
                continue
            prop = set_chunk.GetVariableByName(field_name)
            if prop is None:
                continue
            try:
                setattr(final_set, field_name, _read_cutscene_field_from_prop(field_name, prop, chunks))
            except Exception:
                log.debug("Failed reading W2 cutscene field %s", field_name, exc_info=True)

    final_set.actorsDef = list(actors)
    return final_set


def load_w2_cutscene_template(file_name):
    with open(file_name, "rb") as f:
        raw_data = f.read()
    with open(file_name, "rb") as f:
        the_file = getCR2W(f)
    return create_CCutscene_w2(the_file, raw_data)


def load_w2_cutscene_anim(file_name, rigPath=None, anim_name=None) -> w3_types.CSkeletalAnimationSet:
    """Decode one (or all) animation(s) from a W2 .w2cutscene via Havok."""
    with open(file_name, "rb") as f:
        raw_data = f.read()
    with open(file_name, "rb") as f:
        the_file = getCR2W(f)

    cutscene = create_CCutscene_w2(the_file, raw_data)
    anim_set = w3_types.CSkeletalAnimationSet(list(cutscene.animations))

    fallback_bone_names, fallback_track_names = get_fallback_w2_skeleton_names(
        rigPath,
        source_file=file_name,
    )

    if anim_name:
        target_idx = None
        for idx, entry in enumerate(anim_set.animations):
            if entry.animation and entry.animation.name == anim_name:
                target_idx = idx
                break
        if target_idx is None:
            log.warning("Cutscene animation '%s' not found in %s", anim_name, os.path.basename(file_name))
            anim_set.animations = []
            return anim_set

        blob_start = sum(_w2_entry_part_count(entry) for entry in anim_set.animations[:target_idx])
        part_count = _w2_entry_part_count(anim_set.animations[target_idx])
        decoded_parts = []
        for part_idx in range(part_count):
            decoded_parts.append(
                HavokPackfile.decode_animation_blob_at_index(
                    raw_data,
                    blob_start + part_idx,
                    fallback_bone_names=fallback_bone_names,
                    fallback_track_names=fallback_track_names,
                )
            )
        if any(decoded and getattr(decoded, "buffer", None) for decoded in decoded_parts):
            _apply_decoded_w2_cutscene_parts(
                anim_set.animations[target_idx],
                decoded_parts,
                first_frames=_w2_entry_first_frames(anim_set.animations[target_idx]),
            )
            log.info(
                "Decoded W2 cutscene animation '%s' (blob %d-%d/%d) from %s",
                anim_name,
                blob_start + 1,
                blob_start + part_count,
                sum(_w2_entry_part_count(entry) for entry in anim_set.animations),
                os.path.basename(file_name),
            )
        else:
            log.warning(
                "Failed to decode W2 cutscene animation '%s' (blob %d-%d) from %s",
                anim_name, blob_start, blob_start + part_count - 1, os.path.basename(file_name),
            )
        anim_set.animations = [anim_set.animations[target_idx]]
        return anim_set

    blob_cursor = 0
    for entry in anim_set.animations:
        part_count = _w2_entry_part_count(entry)
        decoded_parts = []
        for part_idx in range(part_count):
            decoded_parts.append(
                HavokPackfile.decode_animation_blob_at_index(
                    raw_data,
                    blob_cursor + part_idx,
                    fallback_bone_names=fallback_bone_names,
                    fallback_track_names=fallback_track_names,
                )
            )
        _apply_decoded_w2_cutscene_parts(
            entry,
            decoded_parts,
            first_frames=_w2_entry_first_frames(entry),
        )
        blob_cursor += part_count
    return anim_set
