import csv
import logging
import math
import re
import socket
import struct
from dataclasses import dataclass
from pathlib import Path

import bpy

from ..action_compat import bind_strip_action_slot, new_action_fcurve, resolve_action_slot
from ..ui import facs_helper


log = logging.getLogger(__name__)

TRACK_NAME = "livelink_face"
HEAD_BONE_CANDIDATES = ("head", "Head", "head_g", "Head_g")
NECK_BONE_CANDIDATES = ("neck", "Neck", "neck1", "Neck1", "neck_g", "Neck_g")
HEAD_CHANNELS = ("HeadYaw", "HeadPitch", "HeadRoll")

# Live Link Face UDP packets use the ARKit blendshape order before the head/eye rotation floats.
STREAM_FACS_ORDER = (
    "eyeBlinkLeft",
    "eyeLookDownLeft",
    "eyeLookInLeft",
    "eyeLookOutLeft",
    "eyeLookUpLeft",
    "eyeSquintLeft",
    "eyeWideLeft",
    "eyeBlinkRight",
    "eyeLookDownRight",
    "eyeLookInRight",
    "eyeLookOutRight",
    "eyeLookUpRight",
    "eyeSquintRight",
    "eyeWideRight",
    "jawForward",
    "jawLeft",
    "jawRight",
    "jawOpen",
    "mouthClose",
    "mouthFunnel",
    "mouthPucker",
    "mouthLeft",
    "mouthRight",
    "mouthSmileLeft",
    "mouthSmileRight",
    "mouthFrownLeft",
    "mouthFrownRight",
    "mouthDimpleLeft",
    "mouthDimpleRight",
    "mouthStretchLeft",
    "mouthStretchRight",
    "mouthRollLower",
    "mouthRollUpper",
    "mouthShrugLower",
    "mouthShrugUpper",
    "mouthPressLeft",
    "mouthPressRight",
    "mouthLowerDownLeft",
    "mouthLowerDownRight",
    "mouthUpperUpLeft",
    "mouthUpperUpRight",
    "browDownLeft",
    "browDownRight",
    "browInnerUp",
    "browOuterUpLeft",
    "browOuterUpRight",
    "cheekPuff",
    "cheekSquintLeft",
    "cheekSquintRight",
    "noseSneerLeft",
    "noseSneerRight",
    "tongueOut",
)


@dataclass
class LiveLinkSample:
    facs: dict
    head_yaw: float = 0.0
    head_pitch: float = 0.0
    head_roll: float = 0.0


@dataclass
class LiveLinkCapture:
    path: str
    samples: list
    facs_channels: list
    has_head: bool


def _normalized_name(name):
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def _read_float(cells, index, default=0.0):
    if index is None or index < 0 or index >= len(cells):
        return float(default)
    try:
        return float(str(cells[index]).strip())
    except (TypeError, ValueError):
        return float(default)


def _mirror_facs_name(name):
    if "Left" in name:
        return name.replace("Left", "Right")
    if "Right" in name:
        return name.replace("Right", "Left")
    return name


def mirror_sample(sample):
    return LiveLinkSample(
        facs={_mirror_facs_name(name): value for name, value in sample.facs.items()},
        head_yaw=-sample.head_yaw,
        head_pitch=sample.head_pitch,
        head_roll=-sample.head_roll,
    )


def _find_pose_bone(armature, candidates):
    if armature is None or getattr(armature, "pose", None) is None:
        return None
    for name in candidates:
        bone = armature.pose.bones.get(name)
        if bone is not None:
            return bone

    lowered = {_normalized_name(name) for name in candidates}
    for bone in armature.pose.bones:
        if _normalized_name(bone.name) in lowered:
            return bone
    return None


def _resolve_start_frame(scene):
    if getattr(scene, "witcher_anim_nla_mode", "REPLACE") == "APPEND_AT_CURSOR":
        return float(scene.frame_current)
    return 0.0


def _track_name_matches(track, track_name):
    current = str(getattr(track, "name", "") or "")
    return current == track_name or current.startswith(f"{track_name}.")


def _remove_matching_nla_tracks(anim_data, track_name):
    removed_actions = []
    if anim_data is None:
        return removed_actions
    for track in list(anim_data.nla_tracks):
        if not _track_name_matches(track, track_name):
            continue
        for strip in list(track.strips):
            action = getattr(strip, "action", None)
            if action is not None and action.name not in removed_actions:
                removed_actions.append(action.name)
            track.strips.remove(strip)
        anim_data.nla_tracks.remove(track)
    return removed_actions


def _remove_orphan_actions(action_names):
    for action_name in action_names:
        action = bpy.data.actions.get(action_name)
        if action is not None and action.users == 0:
            bpy.data.actions.remove(action)


def _add_fcurve_values(action, target, data_path, frames, values, *, index=None, group_name=None):
    fcurve = new_action_fcurve(action, target, data_path=data_path, index=index, group_name=group_name)
    fcurve.keyframe_points.add(len(frames))
    for i, value in enumerate(values):
        key = fcurve.keyframe_points[i]
        key.co = (frames[i], float(value))
        key.interpolation = "LINEAR"
    fcurve.update()
    return fcurve


def _resolve_head_unit(samples, head_units):
    unit = str(head_units or "AUTO").upper()
    if unit in {"DEGREES", "RADIANS"}:
        return unit

    max_abs = 0.0
    for sample in samples:
        max_abs = max(max_abs, abs(sample.head_yaw), abs(sample.head_pitch), abs(sample.head_roll))
    return "DEGREES" if max_abs > 1.5 else "RADIANS"


def _head_radians(sample, unit, rotation_scale=1.0):
    # Witcher head/neck bones face along local -X:
    #   X rotation = roll/tilt, Y rotation = yaw, Z rotation = pitch/up-down.
    values = (sample.head_roll, sample.head_yaw, -sample.head_pitch)
    if unit == "DEGREES":
        values = tuple(math.radians(value) for value in values)
    scale = float(rotation_scale)
    return tuple(value * scale for value in values)


def _split_head_rotation(sample, unit, rotation_scale, neck_share):
    pitch, yaw, roll = _head_radians(sample, unit, rotation_scale)
    neck_factor = max(0.0, min(1.0, float(neck_share)))
    head_factor = 1.0 - neck_factor
    return (
        (pitch * head_factor, yaw * head_factor, roll * head_factor),
        (pitch * neck_factor, yaw * neck_factor, roll * neck_factor),
    )


def read_livelinkface_csv(csv_path):
    path = Path(csv_path)
    if not path.is_file():
        raise RuntimeError(f"Live Link Face CSV not found: {csv_path}")

    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        rows = [row for row in csv.reader(handle) if row and any(str(cell).strip() for cell in row)]
    if len(rows) < 2:
        raise RuntimeError("Live Link Face CSV must contain a header and at least one sample row.")

    header = [str(cell or "").strip() for cell in rows[0]]
    header_lookup = {_normalized_name(name): index for index, name in enumerate(header)}

    facs_lookup = {_normalized_name(name): name for name in facs_helper.get_facs_channels()}
    facs_indices = {}
    for normalized, facs_name in facs_lookup.items():
        if normalized in header_lookup:
            facs_indices[facs_name] = header_lookup[normalized]

    head_indices = {
        "yaw": header_lookup.get(_normalized_name("HeadYaw")),
        "pitch": header_lookup.get(_normalized_name("HeadPitch")),
        "roll": header_lookup.get(_normalized_name("HeadRoll")),
    }
    has_head = any(index is not None for index in head_indices.values())

    if not facs_indices and not has_head:
        raise RuntimeError("CSV did not contain ARKit/FACS or HeadYaw/HeadPitch/HeadRoll columns.")

    samples = []
    for row in rows[1:]:
        facs_values = {
            facs_name: _read_float(row, index)
            for facs_name, index in facs_indices.items()
        }
        samples.append(LiveLinkSample(
            facs=facs_values,
            head_yaw=_read_float(row, head_indices["yaw"]),
            head_pitch=_read_float(row, head_indices["pitch"]),
            head_roll=_read_float(row, head_indices["roll"]),
        ))

    return LiveLinkCapture(
        path=str(path),
        samples=samples,
        facs_channels=sorted(facs_indices.keys()),
        has_head=has_head,
    )


def decode_livelink_udp_packet(packet):
    if len(packet) < 22:
        raise ValueError("Live Link packet is too short.")

    offset = 1
    device_len = struct.unpack_from("!i", packet, offset)[0]
    offset += 4
    if device_len < 0 or offset + device_len + 4 > len(packet):
        raise ValueError("Live Link packet has an invalid device id length.")
    offset += device_len

    name_len = struct.unpack_from("!i", packet, offset)[0]
    offset += 4
    if name_len < 0 or offset + name_len + 17 > len(packet):
        raise ValueError("Live Link packet has an invalid subject name length.")
    offset += name_len

    offset += 16  # frame number, sub-frame, fps, denominator
    blendshape_count = struct.unpack_from("!B", packet, offset)[0]
    offset += 1
    if blendshape_count < 55:
        raise ValueError(f"Live Link packet only contains {blendshape_count} float values.")
    byte_count = blendshape_count * 4
    if offset + byte_count > len(packet):
        raise ValueError("Live Link packet ended before all float values were present.")

    values = struct.unpack_from(f"!{blendshape_count}f", packet, offset)
    facs_values = {
        name: float(values[index])
        for index, name in enumerate(STREAM_FACS_ORDER)
        if index < len(values)
    }
    return LiveLinkSample(
        facs=facs_values,
        head_yaw=float(values[52]),
        head_pitch=float(values[53]),
        head_roll=float(values[54]),
    )


def resolve_target_armature(context):
    from ..ui import ui_voice

    return ui_voice._resolve_voice_target_armature(context)


def ensure_livelink_face_setup(context, armature):
    from ..ui import ui_voice
    from ..ui.armature_context import set_main_armature

    if armature is None:
        raise RuntimeError("No character target armature found.")

    ui_voice._auto_load_face_morphs(context, armature)
    if not ui_voice._armature_has_face_morphs(armature):
        raise RuntimeError("Load Face Morphs on the character before importing Live Link Face.")

    pose_bone = armature.pose.bones.get("w3_face_poses") if armature.pose else None
    if pose_bone is None:
        raise RuntimeError("The target armature is missing the w3_face_poses pose bone.")

    missing_facs = [name for name in facs_helper.get_facs_channels() if name not in pose_bone]
    if missing_facs:
        previous_active = context.view_layer.objects.active
        previous_selection = list(context.selected_objects)
        try:
            for obj in previous_selection:
                obj.select_set(False)
            armature.select_set(True)
            context.view_layer.objects.active = armature
            set_main_armature(context.scene, armature)
            result = bpy.ops.witcher.create_facs()
            if "FINISHED" not in result:
                raise RuntimeError("Create FACS did not finish.")
        finally:
            for obj in context.selected_objects:
                obj.select_set(False)
            for obj in previous_selection:
                if obj.name in bpy.data.objects:
                    obj.select_set(True)
            if previous_active and previous_active.name in bpy.data.objects:
                context.view_layer.objects.active = previous_active

    pose_bone = armature.pose.bones.get("w3_face_poses")
    missing_facs = [name for name in facs_helper.get_facs_channels() if name not in pose_bone]
    if missing_facs:
        raise RuntimeError(f"Create FACS did not add {len(missing_facs)} required ARKit controls.")

    if "facs_enabled" in pose_bone:
        pose_bone["facs_enabled"] = 1.0
    try:
        rig_settings = armature.data.witcherui_RigSettings
        rig_settings.facs_enabled = True
    except Exception:
        pass

    return pose_bone


def apply_sample_to_pose(
    context,
    armature,
    sample,
    *,
    apply_facs=True,
    apply_head=True,
    head_units="AUTO",
    head_rotation_scale=1.0,
    neck_rotation_share=0.35,
    mirror_view=False,
):
    pose_bone = armature.pose.bones.get("w3_face_poses") if armature and armature.pose else None
    if pose_bone is None:
        raise RuntimeError("The target armature is missing the w3_face_poses pose bone.")

    if mirror_view:
        sample = mirror_sample(sample)

    if apply_facs:
        for facs_name, value in sample.facs.items():
            if facs_name in pose_bone:
                pose_bone[facs_name] = float(value)

    if apply_head:
        unit = _resolve_head_unit([sample], head_units)
        head_rotation, neck_rotation = _split_head_rotation(sample, unit, head_rotation_scale, neck_rotation_share)
        head_bone = _find_pose_bone(armature, HEAD_BONE_CANDIDATES)
        neck_bone = _find_pose_bone(armature, NECK_BONE_CANDIDATES)
        for bone, values in ((head_bone, head_rotation), (neck_bone, neck_rotation)):
            if bone is None:
                continue
            bone.rotation_mode = "XYZ"
            bone.rotation_euler[0] = values[0]
            bone.rotation_euler[1] = values[1]
            bone.rotation_euler[2] = values[2]

    armature.update_tag()
    if context is not None and getattr(context, "view_layer", None) is not None:
        context.view_layer.update()


def apply_capture_to_armature(
    context,
    armature,
    capture,
    *,
    action_name=None,
    start_frame=None,
    track_name=TRACK_NAME,
    replace_existing=True,
    apply_facs=True,
    apply_head=True,
    head_units="AUTO",
    head_rotation_scale=1.0,
    neck_rotation_share=0.35,
    zero_head_from_first_frame=False,
    mirror_view=False,
):
    pose_bone = ensure_livelink_face_setup(context, armature)
    samples = list(capture.samples or [])
    if not samples:
        raise RuntimeError("Live Link Face capture contains no samples.")

    if zero_head_from_first_frame:
        first = samples[0]
        samples = [
            LiveLinkSample(
                facs=sample.facs,
                head_yaw=sample.head_yaw - first.head_yaw,
                head_pitch=sample.head_pitch - first.head_pitch,
                head_roll=sample.head_roll - first.head_roll,
            )
            for sample in samples
        ]

    if mirror_view:
        samples = [mirror_sample(sample) for sample in samples]

    if armature.animation_data is None:
        armature.animation_data_create()
    armature.animation_data.use_nla = True

    old_actions = _remove_matching_nla_tracks(armature.animation_data, track_name) if replace_existing else []

    start = _resolve_start_frame(context.scene) if start_frame is None else float(start_frame)
    frame_count = len(samples)
    frames = [start + index for index in range(frame_count)]
    if not action_name:
        action_name = f"LiveLinkFace {Path(capture.path).stem}"
    action = bpy.data.actions.new(name=action_name)
    slot = resolve_action_slot(action, target=armature, ensure=True)

    if apply_facs:
        facs_channels = sorted({_mirror_facs_name(name) if mirror_view else name for name in capture.facs_channels})
        for facs_name in facs_channels:
            if facs_name not in pose_bone:
                continue
            values = [sample.facs.get(facs_name, 0.0) for sample in samples]
            data_path = f'pose.bones["{pose_bone.name}"]["{facs_name}"]'
            _add_fcurve_values(action, armature, data_path, frames, values, group_name="Live Link FACS")

    head_bone = _find_pose_bone(armature, HEAD_BONE_CANDIDATES)
    neck_bone = _find_pose_bone(armature, NECK_BONE_CANDIDATES)
    unit = _resolve_head_unit(samples, head_units)
    keyed_head_bones = []
    if apply_head and capture.has_head:
        rotations = [_split_head_rotation(sample, unit, head_rotation_scale, neck_rotation_share) for sample in samples]
        for bone, tuple_index, group_name in (
            (head_bone, 0, "Live Link Head"),
            (neck_bone, 1, "Live Link Neck"),
        ):
            if bone is None:
                continue
            bone.rotation_mode = "XYZ"
            keyed_head_bones.append(bone.name)
            data_path = f'pose.bones["{bone.name}"].rotation_euler'
            for axis in range(3):
                values = [rotation[tuple_index][axis] for rotation in rotations]
                _add_fcurve_values(
                    action,
                    armature,
                    data_path,
                    frames,
                    values,
                    index=axis,
                    group_name=group_name,
                )

    track = armature.animation_data.nla_tracks.new()
    track.name = track_name
    strip = track.strips.new(action.name, int(start), action)
    bind_strip_action_slot(strip, slot)
    strip.frame_start = start
    strip.frame_end = start + frame_count
    strip.blend_type = "REPLACE"

    _remove_orphan_actions(old_actions)
    context.scene.frame_set(context.scene.frame_current)

    return {
        "start_frame": frames[0],
        "end_frame": frames[-1] + 1,
        "frame_count": frame_count,
        "facs_count": len(capture.facs_channels) if apply_facs else 0,
        "head_bones": keyed_head_bones,
        "head_units": unit,
        "track_name": track.name,
        "action_name": action.name,
    }


class LiveLinkStreamSession:
    def __init__(
        self,
        *,
        armature_name,
        udp_port,
        apply_facs=True,
        apply_head=True,
        head_units="AUTO",
        head_rotation_scale=1.0,
        neck_rotation_share=0.35,
        mirror_view=False,
    ):
        self.armature_name = armature_name
        self.udp_port = int(udp_port)
        self.apply_facs = bool(apply_facs)
        self.apply_head = bool(apply_head)
        self.head_units = head_units
        self.head_rotation_scale = float(head_rotation_scale)
        self.neck_rotation_share = float(neck_rotation_share)
        self.mirror_view = bool(mirror_view)
        self.socket = None
        self.running = False
        self.last_error = ""

    def start(self):
        if self.running:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        sock.bind(("", self.udp_port))
        self.socket = sock
        self.running = True
        bpy.app.timers.register(self._timer, first_interval=0.01)

    def stop(self):
        self.running = False
        if self.socket is not None:
            try:
                self.socket.close()
            except OSError:
                pass
            self.socket = None

    def _timer(self):
        if not self.running:
            self.stop()
            return None

        armature = bpy.data.objects.get(self.armature_name)
        if armature is None:
            self.last_error = "Target armature no longer exists."
            self.stop()
            return None

        for _unused in range(12):
            try:
                packet, _addr = self.socket.recvfrom(8192)
            except BlockingIOError:
                break
            except OSError as exc:
                self.last_error = str(exc)
                self.stop()
                return None

            try:
                sample = decode_livelink_udp_packet(packet)
                scene = bpy.context.scene
                apply_sample_to_pose(
                    bpy.context,
                    armature,
                    sample,
                    apply_facs=getattr(scene, "witcher_livelink_stream_facs", self.apply_facs),
                    apply_head=getattr(scene, "witcher_livelink_stream_head", self.apply_head),
                    head_units=getattr(scene, "witcher_livelink_head_units", self.head_units),
                    head_rotation_scale=getattr(scene, "witcher_livelink_head_scale", self.head_rotation_scale),
                    neck_rotation_share=getattr(scene, "witcher_livelink_neck_share", self.neck_rotation_share),
                    mirror_view=getattr(scene, "witcher_livelink_mirror_view", self.mirror_view),
                )
            except Exception as exc:
                self.last_error = str(exc)
                log.debug("Live Link stream packet skipped: %s", exc)

        return 0.01
