import math
from pathlib import Path


def scene_fps(scene):
    render = getattr(scene, "render", None)
    fps = float(getattr(render, "fps", 30.0) or 30.0)
    fps_base = float(getattr(render, "fps_base", 1.0) or 1.0)
    return fps / fps_base if fps_base else fps


def _segment_sample_value(frame, start_frame, end_frame, ramp_frames):
    sample_frame = frame + 0.5
    if sample_frame < start_frame or sample_frame > end_frame:
        return 0.0

    value = 1.0
    if ramp_frames > 0.0:
        if sample_frame < start_frame + ramp_frames:
            value = min(value, max(0.0, (sample_frame - start_frame) / ramp_frames))
        if sample_frame > end_frame - ramp_frames:
            value = min(value, max(0.0, (end_frame - sample_frame) / ramp_frames))
    return max(0.0, min(1.0, value))


def build_phoneme_values(segments, phoneme_list, fps, start_frame, strength=1.0, ramp_frames=1.0):
    if not segments:
        raise ValueError("No phoneme segments to apply.")

    phoneme_index = {phoneme: idx for idx, phoneme in enumerate(phoneme_list)}
    end_ms = max(segment.end_ms for segment in segments)
    first_frame = int(math.floor(start_frame))
    last_frame = int(math.ceil(start_frame + (end_ms / 1000.0) * fps + 2.0))
    frames = list(range(first_frame, max(first_frame + 1, last_frame + 1)))
    values = [[0.0 for _frame in frames] for _phoneme in phoneme_list]

    strength = max(0.0, float(strength))
    for segment in segments:
        idx = phoneme_index.get(segment.phoneme)
        if idx is None:
            continue

        seg_start = start_frame + (segment.start_ms / 1000.0) * fps
        seg_end = start_frame + (segment.end_ms / 1000.0) * fps
        if seg_end <= seg_start:
            seg_end = seg_start + 1.0

        target = max(0.0, min(1.0, float(segment.weight) * strength))
        for offset, frame in enumerate(frames):
            sample = _segment_sample_value(frame, seg_start, seg_end, ramp_frames)
            if sample <= 0.0:
                continue
            values[idx][offset] = max(values[idx][offset], target * sample)

    return frames, values


def apply_segments_to_armature(context, armature, segments, action_name, start_frame=0.0, strength=1.0):
    from ..ui import phoneme_helper
    from ..ui.ui_voice import _apply_phoneme_action

    _phonemes_data, _morphs_data, phoneme_list, _morph_list = phoneme_helper.read_phoneme_weights()
    if not phoneme_list:
        raise RuntimeError("phonemes.txt did not contain any phoneme data.")

    pose_bone = armature.pose.bones.get("w3_face_poses") if armature and armature.pose else None
    if pose_bone is None:
        raise RuntimeError("The target armature is missing the w3_face_poses pose bone.")

    fps = scene_fps(context.scene)
    frames, phoneme_values = build_phoneme_values(
        segments,
        phoneme_list,
        fps,
        float(start_frame),
        strength=strength,
    )

    pose_bone["phoneme_enabled"] = 1.0
    try:
        rig_settings = armature.data.witcherui_RigSettings
        rig_settings.phoneme_enabled = True
    except Exception:
        pass

    _apply_phoneme_action(
        armature,
        pose_bone,
        phoneme_list,
        frames,
        phoneme_values,
        action_name=action_name,
        track_name="voice_import_phoneme",
    )
    context.scene.frame_set(context.scene.frame_current)

    return {
        "start_frame": frames[0],
        "end_frame": frames[-1] + 1,
        "frame_count": len(frames),
        "phoneme_count": len(segments),
    }


def read_lipsyncanim_csv(csv_path):
    csv_path = Path(csv_path)
    metadata = {}
    morph_values = {}
    with open(csv_path, "r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith(";meta[") and line.endswith("]"):
                payload = line[len(";meta["):-1]
                if "=" in payload:
                    key, value = payload.split("=", 1)
                    metadata[key.strip()] = value.strip()
                continue
            if line.startswith(";"):
                continue

            cells = line.split(";")
            morph_name = cells[0].strip()
            if not morph_name:
                continue
            values = []
            for cell in cells[1:]:
                cell = cell.strip()
                if not cell:
                    continue
                try:
                    values.append(float(cell))
                except ValueError:
                    values.append(0.0)
            if values:
                morph_values[morph_name] = values

    if not morph_values:
        raise ValueError(f"No morph curves found in {csv_path}")

    frame_count = max(len(values) for values in morph_values.values())
    for values in morph_values.values():
        if len(values) < frame_count:
            values.extend([0.0] * (frame_count - len(values)))
    return metadata, morph_values


def _track_name_matches(track, track_name):
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
    import bpy

    for action_name in action_names or []:
        action = bpy.data.actions.get(action_name)
        if action and action.users == 0:
            bpy.data.actions.remove(action)


def apply_lipsyncanim_csv_to_armature(
    context,
    armature,
    csv_path,
    action_name,
    start_frame=0.0,
    track_name="voice_import",
):
    import bpy
    from ..action_compat import bind_strip_action_slot, new_action_fcurve, resolve_action_slot

    metadata, morph_values = read_lipsyncanim_csv(csv_path)
    pose_bone = armature.pose.bones.get("w3_face_poses") if armature and armature.pose else None
    if pose_bone is None:
        raise RuntimeError("The target armature is missing the w3_face_poses pose bone.")

    if armature.animation_data is None:
        armature.animation_data_create()
    armature.animation_data.use_nla = True

    old_actions = []
    old_actions.extend(_remove_nla_tracks(armature.animation_data, track_name))
    old_actions.extend(_remove_nla_tracks(armature.animation_data, "voice_import_phoneme"))

    pose_bone["phoneme_enabled"] = 0.0
    try:
        rig_settings = armature.data.witcherui_RigSettings
        rig_settings.phoneme_enabled = False
    except Exception:
        pass

    action = bpy.data.actions.new(name=action_name)
    start_frame = float(start_frame)
    frame_count = max(len(values) for values in morph_values.values())
    frames = [start_frame + idx for idx in range(frame_count)]

    for morph_name, values in morph_values.items():
        if morph_name not in pose_bone:
            pose_bone[morph_name] = 0.0
        try:
            pose_bone.id_properties_ui(morph_name).update(min=-1.0, max=1.0, soft_min=0.0, soft_max=1.0)
        except Exception:
            pass

        data_path = f'pose.bones["{pose_bone.name}"]["{morph_name}"]'
        fcurve = new_action_fcurve(action, armature, data_path=data_path)
        fcurve.keyframe_points.add(frame_count)
        for idx, value in enumerate(values):
            key = fcurve.keyframe_points[idx]
            key.co = (frames[idx], float(value))
            key.interpolation = "LINEAR"
        fcurve.update()

    track = armature.animation_data.nla_tracks.new()
    track.name = track_name
    strip = track.strips.new(action.name, int(start_frame), action)
    bind_strip_action_slot(strip, resolve_action_slot(action, target=armature, ensure=True))
    strip.frame_start = start_frame
    strip.frame_end = start_frame + frame_count
    strip.blend_type = "REPLACE"

    _remove_orphan_actions(old_actions)
    context.scene.frame_set(context.scene.frame_current)

    return {
        "start_frame": frames[0],
        "end_frame": frames[-1] + 1,
        "frame_count": frame_count,
        "morph_count": len(morph_values),
        "metadata": metadata,
    }
