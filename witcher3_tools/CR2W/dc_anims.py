import logging
log = logging.getLogger(__name__)
import os
import struct
import math
from .dc_skeleton import create_CMimicFace, create_Skeleton
from .common_blender import repo_file
from .CR2W_types import getCR2W
from .havok_parser import HavokPackfile
from . import w3_types
from .w3_types import ( Track, w2AnimsFrames, Quaternion, Vector3D )
from .bin_helpers import (ReadUlong40, ReadUlong48, ReadVLQInt32, readUShort, ReadBit6,
                        readFloat,
                        ReadFloat24,
                        ReadFloat16,
                        readUByteCheck)
from .bStream import *

class CVector3D:
    def __init__(self, f, compression = 0):
        self.x = 0.0
        self.y = 0.0
        self.z = 0.0
        if (compression == 0):
            self.x = readFloat(f)
            self.y = readFloat(f)
            self.z = readFloat(f)
        if (compression == 1):
            self.x = ReadFloat24(f)
            self.y = ReadFloat24(f)
            self.z = ReadFloat24(f)
        if (compression == 2):
            self.x = ReadFloat16(f)
            self.y = ReadFloat16(f)
            self.z = ReadFloat16(f)
    def getList(self):
        return [self.x, self.y, self.z]

class ReadCompressFloat():
    def __init__(self, f, compression):
        val = 0
        if (compression == 0):
            val = readFloat(f)
        if (compression == 1):
            val = ReadFloat24(f)
        if (compression == 2):
            val = ReadFloat16(f)
        self.val = val


def _safe_track_name(skeleton_file, idx):
    if not skeleton_file:
        return idx
    tracks = getattr(skeleton_file, "tracks", None)
    if tracks is None:
        return idx
    if 0 <= idx < len(tracks):
        return tracks[idx]
    if idx == len(tracks):
        log.warning(
            "Animation track index %d exceeds skeleton track count %d; using numeric track ids (possible face/body rig mismatch).",
            idx,
            len(tracks),
        )
    return idx

def create_lipsync_anim(file, Skeleton_file):
    CHUNKS = file.CHUNKS.CHUNKS
    bones = []
    tracks = []

    for chunk in CHUNKS:
        if chunk.name == "CSkeletalAnimation":
            CSkeletalAnimation = chunk
        if chunk.name == "CAnimationBufferBitwiseCompressed":
            CAnimationBufferBitwiseCompressed = chunk
    return create_anim(file, CSkeletalAnimation, CAnimationBufferBitwiseCompressed, Skeleton_file)

#######################
#### Lipsync File #####
#######################

class TrackPointer:
    def __init__(self, dt, compression, zero, numFrames, dataAddr, dataAddrFallback):
        self.dt = dt
        self.compression = compression
        self.zero = zero
        self.numFrames = numFrames
        self.dataAddr = dataAddr
        self.dataAddrFallback = dataAddrFallback

    def __str__(self):
        return f"Track: numFrames = {self.numFrames}, dataAddr = {self.dataAddr}"

def read_lipsync_track(file):
    data = file.read(struct.calcsize("fbbHII"))
    dt, compression, zero, numFrames, dataAddr, dataAddrFallback = struct.unpack("fbbHII", data)
    return TrackPointer(dt, compression, zero, numFrames, dataAddr, dataAddrFallback)

def read_lipsync_file(filename):
    with open(filename, "rb") as file:
        # Read the initial part of the file
        frames_per_second = struct.unpack("f", file.read(4))[0]
        duration = struct.unpack("f", file.read(4))[0]
        compression = ReadBit6(file) #struct.unpack("b", file.read(1))[0]
        bones = [read_lipsync_track(file) for _ in range(4)]
        tracks_size = ReadVLQInt32(file)
        tracks = [read_lipsync_track(file) for _ in range(tracks_size)]
        bin_size = ReadVLQInt32(file)
        binary_data = file.read(bin_size) if bin_size > 0 else b''
        numFrames = struct.unpack("I", file.read(4))[0]  # Read numFrames (uint32)
        dt = struct.unpack("f", file.read(4))[0]         # Read dt (float)

        return {
            "frames_per_second": frames_per_second,
            "duration": duration,
            "compression": compression,
            "bones": bones,
            "tracks": tracks,
            "binary_data": binary_data,
            "numFrames": numFrames,
            "dt": dt
        }

def read_lipsync_buffer_file(fileName, Skeleton_file):
    buffer_file = read_lipsync_file(fileName)
    data_in_file = buffer_file['binary_data']
    bones, tracks = [], []
    b = bytearray(data_in_file)
    the_data = bStream(data = b)
    for (idx, track) in enumerate(buffer_file["tracks"]):
        this_track = Track(idx,
            trackName = _safe_track_name(Skeleton_file, idx),
            numFrames = "",
            dt = "",
            trackFrames = [])
        trackData = track
        this_track.dt = trackData.dt
        this_track.numFrames = trackData.numFrames
        compression = trackData.compression
        dataAddr = trackData.dataAddr
        dataAddrFallback = trackData.dataAddrFallback
        the_data.seek(dataAddr)
        for _ in range(0, this_track.numFrames):
            this_track.trackFrames.append(ReadCompressFloat(the_data, compression).val)
        tracks.append(this_track)
    buffer = w3_types.CAnimationBufferBitwiseCompressed(bones, tracks, duration=buffer_file['duration'], numFrames=buffer_file['numFrames'], dt=buffer_file['dt'])

    anim = w3_types.CSkeletalAnimation(name ="lipsync", duration=buffer_file['duration'], framesPerSecond=buffer_file['frames_per_second'], animBuffer=buffer, motionExtraction={}, SkeletalAnimationType = "SAT_Normal", AdditiveType=None)
    return anim

def read_uncooked_anim_buffer(embedded_data, CAnimationBufferBitwiseCompressed, duration, Skeleton_file) -> w3_types.CAnimationBufferBitwiseCompressed:
    """
    Parse embedded animation data from uncooked .w2anims files.

    The uncooked format stores animation data with a hybrid structure:
    - Header (20 bytes): version, dt, unknown, zeros, duration_factor
    - Metadata section (9 bytes): markers and numFrames
    - Single-frame bone data with 0x01 prefix per float
    - Multi-frame data blocks grouped together with 0x00 prefix

    Important: Multi-frame data is grouped (all multi-frame position, then all multi-frame
    orientation) rather than stored per-bone. This requires a two-pass parsing approach.
    """
    if not embedded_data or len(embedded_data) < 20:
        log.warning(f"Embedded data too small ({len(embedded_data) if embedded_data else 0} bytes)")
        return w3_types.CAnimationBufferBitwiseCompressed([], [], duration=duration if duration else 0.0, numFrames=0, dt=0.0)

    # Debug: show first 30 bytes as hex
    hex_preview = ' '.join(f'{b:02x}' for b in embedded_data[:min(30, len(embedded_data))])
    log.info(f"Embedded data first 30 bytes: {hex_preview}")

    # Check if data starts with a length prefix (the first 4 bytes might be the size)
    first_uint = struct.unpack_from('<I', embedded_data, 0)[0]
    log.info(f"First uint32: {first_uint} (data size: {len(embedded_data)})")

    # Parse header - check if we need to skip a length prefix
    data_offset = 0
    if first_uint == len(embedded_data) - 4 or first_uint == len(embedded_data):
        # First 4 bytes are a length prefix, skip them
        data_offset = 4
        log.info(f"Detected length prefix, adjusting offset to {data_offset}")
    elif first_uint > 1000000 or first_uint == 0:
        # First value doesn't look like a valid version, might have other prefix
        # Try to find the header pattern: version (small number), dt (~0.033), etc.
        for test_offset in [0, 2, 4, 6, 8]:
            if test_offset + 8 <= len(embedded_data):
                test_version = struct.unpack_from('<I', embedded_data, test_offset)[0]
                test_dt = struct.unpack_from('<f', embedded_data, test_offset + 4)[0]
                if 0 < test_version < 100 and 0.01 < test_dt < 0.2:
                    data_offset = test_offset
                    log.info(f"Found header at offset {data_offset}: version={test_version}, dt={test_dt}")
                    break

    # Parse header - the embedded data has its own header with correct values
    # Header structure (29 bytes total):
    #   0-3:   version (uint32) - typically 3
    #   4-7:   dt (float) - time delta per frame
    #   8-11:  unknown (uint32)
    #   12-15: zeros
    #   16-19: duration factor (float)
    #   20-21: flags (2 bytes)
    #   22-24: padding (3 bytes)
    #   25-28: numFrames (uint32) - ACTUAL frame count for multi-frame data

    version = struct.unpack_from('<I', embedded_data, data_offset)[0]
    dt = struct.unpack_from('<f', embedded_data, data_offset + 4)[0]
    if dt <= 0 or dt > 1.0:
        dt = 0.0333333  # Default to ~30fps

    # Read numFrames from embedded header at offset 25 - this is the authoritative value
    # The CR2W chunk metadata may have different (compressed) frame counts
    buffer_numFrames = struct.unpack_from('<I', embedded_data, data_offset + 25)[0]

    # Sanity check - if embedded numFrames seems wrong, fall back to chunk metadata
    if buffer_numFrames == 0 or buffer_numFrames > 10000:
        if CAnimationBufferBitwiseCompressed is not None:
            nf_prop = CAnimationBufferBitwiseCompressed.GetVariableByName('numFrames')
            if nf_prop is not None and nf_prop.Value > 0:
                buffer_numFrames = nf_prop.Value
                log.warning(f"Embedded numFrames invalid, using chunk value: {buffer_numFrames}")
        if buffer_numFrames == 0 or buffer_numFrames > 10000:
            buffer_numFrames = int(duration / dt) + 1 if dt > 0 else 30

    log.info(f"Uncooked animation: version={version}, dt={dt:.6f}, duration={duration}, frames={buffer_numFrames}, data size={len(embedded_data)} bytes")

    bones = []
    bones_prop = CAnimationBufferBitwiseCompressed.GetVariableByName('bones')
    if bones_prop is None or not hasattr(bones_prop, 'More'):
        log.warning("No bone metadata found in animation buffer")
        return w3_types.CAnimationBufferBitwiseCompressed([], [], duration=duration if duration else 0.0, numFrames=buffer_numFrames, dt=dt)

    def normalize_quat(x, y, z, w):
        """Normalize quaternion, handling edge cases."""
        if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z) and math.isfinite(w)):
            return (0.0, 0.0, 0.0, 1.0)
        mag = math.sqrt(x * x + y * y + z * z + w * w)
        if mag <= 1e-8 or not math.isfinite(mag):
            return (0.0, 0.0, 0.0, 1.0)
        return (x / mag, y / mag, z / mag, w / mag)

    def read_single_float(data, pos):
        """Read a single float with 0x01 prefix. Returns (value, new_pos, success)."""
        if pos >= len(data):
            return 0.0, pos, False
        if data[pos] == 0x01 and pos + 5 <= len(data):
            val = struct.unpack_from('<f', data, pos + 1)[0]
            if math.isfinite(val):
                return val, pos + 5, True
        return 0.0, pos, False

    def read_multi_component_planar(data, pos, num_frames, num_components):
        """
        Read multi-frame data in PLANAR format.
        Format: 0x00 + N floats for component 0, 0x00 + N floats for component 1, etc.
        Returns (list of frames [[c0,c1,c2], ...], new_pos, success).
        """
        components = []
        for c in range(num_components):
            if pos >= len(data) or data[pos] != 0x00:
                byte_val = data[pos] if pos < len(data) else 0
                log.debug(f"Expected 0x00 at {pos} for component {c}, got 0x{byte_val:02x}")
                return None, pos, False
            pos += 1  # skip 0x00 marker
            vals = []
            for _ in range(num_frames):
                if pos + 4 <= len(data):
                    val = struct.unpack_from('<f', data, pos)[0]
                    vals.append(val)
                    pos += 4
                else:
                    vals.append(0.0)
            components.append(vals)
        
        # Transpose from planar [component][frame] to interleaved [frame][component]
        frames = []
        for i in range(num_frames):
            frame = [components[c][i] for c in range(num_components)]
            frames.append(frame)
        return frames, pos, True

    def read_track_values(data, pos, expected_num_frames, num_components, track_name="track", default_values=None):
        """
        Read track values from embedded data.

        IMPORTANT: Format is detected PER-COMPONENT, not per-track!
        Each component can independently be:
        - 0x01 = single-frame (one float with 0x01 prefix)
        - 0x00 = multi-frame (0x00 + N floats)

        This allows hybrid tracks like: X=single, Y=multi, Z=multi, W=multi

        Returns (frames_list, new_pos, actual_frame_count, success)
        """
        if pos >= len(data):
            log.debug(f"{track_name}: position {pos} beyond data length {len(data)}")
            return [list(default_values) if default_values else [0.0] * num_components], pos, 1, False

        # Read each component, detecting format per-component
        component_data = []  # List of (values_list, is_multi) per component
        max_frames = 1
        all_success = True

        for comp_idx in range(num_components):
            if pos >= len(data):
                log.debug(f"{track_name}: ran out of data at component {comp_idx}")
                all_success = False
                break

            marker = data[pos]

            if marker == 0x01:
                # Single-frame: 0x01 + 1 float
                if pos + 5 <= len(data):
                    val = struct.unpack_from('<f', data, pos + 1)[0]
                    if math.isfinite(val):
                        component_data.append(([val], False))
                        pos += 5
                    else:
                        component_data.append(([default_values[comp_idx] if default_values else 0.0], False))
                        pos += 5
                        all_success = False
                else:
                    component_data.append(([default_values[comp_idx] if default_values else 0.0], False))
                    all_success = False
                    break

            elif marker == 0x00:
                # Multi-frame: 0x00 + N floats
                pos += 1  # Skip marker
                vals = []
                for _ in range(buffer_numFrames):
                    if pos + 4 <= len(data):
                        val = struct.unpack_from('<f', data, pos)[0]
                        vals.append(val if math.isfinite(val) else 0.0)
                        pos += 4
                    else:
                        vals.append(0.0)
                component_data.append((vals, True))
                max_frames = max(max_frames, len(vals))

            else:
                # Unknown marker - use default for this component
                log.warning(f"{track_name}: unexpected marker 0x{marker:02x} at pos {pos} for component {comp_idx}")
                component_data.append(([default_values[comp_idx] if default_values else 0.0], False))
                all_success = False
                # Try to continue - don't advance pos since we don't know the format
                break

        # If we didn't read all components, fill with defaults
        while len(component_data) < num_components:
            def_val = default_values[len(component_data)] if default_values and len(default_values) > len(component_data) else 0.0
            component_data.append(([def_val], False))

        # Build frames - expand single-frame components to match max_frames
        frames = []
        for frame_idx in range(max_frames):
            frame = []
            for comp_idx in range(num_components):
                vals, is_multi = component_data[comp_idx]
                if is_multi and frame_idx < len(vals):
                    frame.append(vals[frame_idx])
                else:
                    # Single-frame or out of range - use first value
                    frame.append(vals[0] if vals else 0.0)
            frames.append(frame)

        return frames, pos, max_frames, all_success

    # Bone data starts at fixed offset 29 from data_offset
    # Header structure is fixed: 20 bytes header + 9 bytes metadata = 29 bytes
    curr_off = data_offset + 29

    # Validate: first byte should be 0x00 (multi-frame) or 0x01 (single-frame)
    if curr_off < len(embedded_data):
        first_marker = embedded_data[curr_off]
        if first_marker not in (0x00, 0x01):
            log.warning(f"Unexpected first marker 0x{first_marker:02x} at offset {curr_off}, expected 0x00 or 0x01")
    log.info(f"Starting bone data at offset {curr_off}")

    # Process each bone - data is sequential: pos, ori, scale for each bone
    for (idx, bone_meta) in enumerate(bones_prop.More):
        if Skeleton_file and idx >= Skeleton_file.nbBones:
            break

        bone_name = Skeleton_file.names[idx] if Skeleton_file else f"Bone_{idx}"

        # Get track metadata from chunk (WARNING: these are for COMPRESSED data!)
        pos_meta = getattr(bone_meta, 'position', None)
        ori_meta = getattr(bone_meta, 'orientation', None)
        scale_meta = getattr(bone_meta, 'scale', None)

        # The chunk metadata has numFrames for the COMPRESSED format (e.g., 13 frames at dt=0.1)
        # But the embedded uncompressed data uses DIFFERENT frame counts (e.g., 35 frames at dt=0.033)
        # We need to detect multi-frame vs single-frame, then use the embedded header's numFrames
        chunk_pos_nf = pos_meta.GetVariableByName('numFrames').Value if pos_meta and pos_meta.GetVariableByName('numFrames') else 1
        chunk_ori_nf = ori_meta.GetVariableByName('numFrames').Value if ori_meta and ori_meta.GetVariableByName('numFrames') else 1
        chunk_scale_nf = scale_meta.GetVariableByName('numFrames').Value if scale_meta and scale_meta.GetVariableByName('numFrames') else 1

        # IMPORTANT: If chunk says multi-frame (>1), use embedded header's numFrames instead
        # Single-frame tracks (=1) stay as 1
        pos_nf = buffer_numFrames if chunk_pos_nf > 1 else 1
        ori_nf = buffer_numFrames if chunk_ori_nf > 1 else 1
        scale_nf = buffer_numFrames if chunk_scale_nf > 1 else 1

        # Use the embedded header's dt for all tracks (not chunk metadata)
        pos_dt = dt
        ori_dt = dt
        scale_dt = dt

        # Debug: show what we're about to read
        if idx < 5:
            next_byte = embedded_data[curr_off] if curr_off < len(embedded_data) else 0
            log.info(f"Bone {idx} ({bone_name}): offset={curr_off}, next_byte=0x{next_byte:02x}, pos_nf={pos_nf}, ori_nf={ori_nf}, scale_nf={scale_nf}")

        # Read position track (3 components per frame)
        pos_start_off = curr_off
        temp_pos_frames, curr_off, actual_pos_nf, pos_ok = read_track_values(
            embedded_data, curr_off, pos_nf, 3, f"Bone {idx} position",
            default_values=[0.0, 0.0, 0.0])

        # Read orientation track (4 components per frame)
        ori_start_off = curr_off
        temp_ori_frames, curr_off, actual_ori_nf, ori_ok = read_track_values(
            embedded_data, curr_off, ori_nf, 4, f"Bone {idx} orientation",
            default_values=[0.0, 0.0, 0.0, 1.0])

        # Read scale track (3 components per frame)
        scale_start_off = curr_off
        temp_scale_frames, curr_off, actual_scale_nf, scale_ok = read_track_values(
            embedded_data, curr_off, scale_nf, 3, f"Bone {idx} scale",
            default_values=[1.0, 1.0, 1.0])

        # Validate orientation - check if it looks like a valid quaternion
        # Valid quaternion: magnitude should be close to 1, all components finite and in -1 to 1 range
        def validate_quaternion(quat):
            if len(quat) < 4:
                return False
            x, y, z, w = quat
            if not all(math.isfinite(v) for v in quat):
                return False
            # All components should be in valid range for a quaternion
            if not all(-1.1 <= v <= 1.1 for v in quat):
                return False
            mag = math.sqrt(x*x + y*y + z*z + w*w)
            # Magnitude should be close to 1 (0.9 to 1.1)
            return 0.9 < mag < 1.1

        # Validate scale - components should be positive and reasonable, not all zero
        def validate_scale(scale):
            if len(scale) < 3:
                return False
            if not all(math.isfinite(v) and -10.0 < v < 10.0 for v in scale):
                return False
            # At least one component should be non-zero (not all zeros)
            return any(abs(v) > 0.001 for v in scale)

        # Check orientation validity
        if temp_ori_frames and not validate_quaternion(temp_ori_frames[0]):
            log.debug(f"Bone {idx}: invalid orientation {temp_ori_frames[0]}, using identity")
            temp_ori_frames = [[0.0, 0.0, 0.0, 1.0]]
            ori_ok = False

        # Check scale validity
        if temp_scale_frames and not validate_scale(temp_scale_frames[0]):
            log.debug(f"Bone {idx}: invalid scale {temp_scale_frames[0]}, using unit scale")
            temp_scale_frames = [[1.0, 1.0, 1.0]]
            scale_ok = False

        # Debug output for first few bones or problematic ones
        if idx < 3 or not (pos_ok and ori_ok and scale_ok):
            status = "OK" if (pos_ok and ori_ok and scale_ok) else "ISSUES"
            log.info(f"Bone {idx} ({bone_name}): pos={actual_pos_nf}/{pos_nf}, ori={actual_ori_nf}/{ori_nf}, scale={actual_scale_nf}/{scale_nf} [{status}]")
            if idx == 0:
                log.info(f"  Position read from {pos_start_off}: {temp_pos_frames[0] if temp_pos_frames else 'none'}")
                log.info(f"  Orientation read from {ori_start_off}: {temp_ori_frames[0] if temp_ori_frames else 'none'}")
                log.info(f"  Scale read from {scale_start_off}: {temp_scale_frames[0] if temp_scale_frames else 'none'}")

        # Convert orientation frames to Quaternion objects and normalize.
        temp_rot_frames = []
        for frame in temp_ori_frames:
            if len(frame) >= 4:
                nx, ny, nz, nw = normalize_quat(frame[0], frame[1], frame[2], frame[3])
                temp_rot_frames.append(Quaternion(nx, ny, nz, nw))
            else:
                temp_rot_frames.append(Quaternion(0.0, 0.0, 0.0, 1.0))

        # Ensure at least one frame
        if not temp_pos_frames:
            temp_pos_frames = [[0.0, 0.0, 0.0]]
        if not temp_rot_frames:
            temp_rot_frames = [Quaternion(0.0, 0.0, 0.0, 1.0)]
        if not temp_scale_frames:
            temp_scale_frames = [[1.0, 1.0, 1.0]]

        # Create bone animation data
        this_bone = w2AnimsFrames(
            id=idx,
            BoneName=bone_name,
            position_dt=pos_dt,
            position_numFrames=len(temp_pos_frames),
            positionFrames=temp_pos_frames,
            rotation_dt=ori_dt,
            rotation_numFrames=len(temp_rot_frames),
            rotationFrames=temp_rot_frames,
            scale_dt=scale_dt,
            scale_numFrames=len(temp_scale_frames),
            scaleFrames=temp_scale_frames,
            rotationFramesQuat=temp_rot_frames
        )

        bones.append(this_bone)

    log.info(f"Uncooked animation parsed: {len(bones)} bones, final offset {curr_off}/{len(embedded_data)}")
    buffer = w3_types.CAnimationBufferBitwiseCompressed(bones, [], duration=duration, numFrames=buffer_numFrames, dt=dt)
    # Metadata: uncooked files use embedded uncompressed animation data
    buffer._source = "uncompressed"
    buffer._source_detail = "embeddedAnimData"
    return buffer

def read_anim_buffer(file, CAnimationBufferBitwiseCompressed, duration, Skeleton_file, embedded_data=None) -> w3_types.CAnimationBufferBitwiseCompressed:
    # Check if animation buffer chunk has no properties at all (edge case)
    if not CAnimationBufferBitwiseCompressed.PROPS or len(CAnimationBufferBitwiseCompressed.PROPS) == 0:
        log.warning("Animation buffer has no properties")
        return w3_types.CAnimationBufferBitwiseCompressed([], [], duration=duration if duration else 0.0, numFrames=0, dt=0.0)
    bones = []
    tracks = []

    #BUFFER PART
    chunk = CAnimationBufferBitwiseCompressed
    buffer_duration = chunk.GetVariableByName('duration')
    if buffer_duration is not None:
        buffer_duration = chunk.GetVariableByName('duration').Value
    else:
        buffer_duration = duration
    buffer_numFrames = chunk.GetVariableByName('numFrames').Value
    
    #some addatives don't have this?
    buffer_dt = chunk.GetVariableByName('dt').Value if chunk.GetVariableByName('dt') else None

    # Fallback: some cooked files have numFrames=0 in the chunk but correct duration/dt
    if buffer_numFrames == 0 and buffer_dt and buffer_dt > 0 and buffer_duration and buffer_duration > 0:
        buffer_numFrames = round(buffer_duration / buffer_dt) + 1
        log.debug("numFrames was 0 in chunk, computed from duration/dt: %d", buffer_numFrames)

    compressionSettings = chunk.GetVariableByName('compressionSettings')
    if compressionSettings is not None:
        orientationCompressionMethod = compressionSettings.GetVariableByName('orientationCompressionMethod').Index.String
    else:
        orientationCompressionMethod = chunk.GetVariableByName('orientationCompressionMethod')
        if orientationCompressionMethod is not None:
            orientationCompressionMethod = chunk.GetVariableByName('orientationCompressionMethod').Index.String
        else:
            orientationCompressionMethod = "ABOCM_PackIn64bitsW"

    def _bytes_per_component(compression):
        if compression == 2:
            return 2
        if compression == 1:
            return 3
        return 4

    def _bytes_per_vec3(compression):
        return 3 * _bytes_per_component(compression)

    def _bytes_per_quat(method):
        if method and "ABOCM_PackIn48bitsW" in method:
            return 6
        if method and "ABOCM_PackIn64bitsW" in method:
            return 8
        if method and "ABOCM_PackIn40bitsW" in method:
            return 5
        # AsFloat_XYZSignedWInLastBit stores X, Y, Z as float32
        return 12
    
    the_data = None
    the_fallback_data = None
    source_kind = "compressed"
    source_detail = ""

    deferredData = chunk.GetVariableByName("deferredData")
    streamingOption = chunk.GetVariableByName("streamingOption")
    _stream_opt = streamingOption.Index.String if streamingOption else "None"
    log.debug(f"[anim_buffer] duration={buffer_duration}, numFrames={buffer_numFrames}, dt={buffer_dt}, orientMethod={orientationCompressionMethod}, streaming={_stream_opt}")

    # Check for fallbackData (stores first-frame pose data for streaming fallback)
    fallbackData_prop = chunk.GetVariableByName("fallbackData")
    if fallbackData_prop is not None and hasattr(fallbackData_prop, 'value') and fallbackData_prop.value:
        the_fallback_data = bStream(data = bytearray(fallbackData_prop.value))

    # For cooked animations, data is stored in external .buffer files
    # For uncooked animations (e.g., from REDkit), data may be in 'data' property or 'fallbackData'
    if (deferredData is not None and deferredData.ValueA != 0):
        def_path = file.fileName + "." + str(deferredData.ValueA) + ".buffer"

        # Ensure the buffer file exists before loading.
        # If missing and the file is under the uncook path, extract all missing
        # buffers in one pass.  External files (mod projects) get a clear error.
        if not os.path.exists(def_path):
            from .common_blender import extract_missing_buffers
            extract_missing_buffers(file.fileName)

            if not os.path.exists(def_path):
                raise FileNotFoundError(
                    f"Missing buffer file: {def_path}\n"
                    f"Animation requires this buffer but it could not be found or extracted.\n"
                    f"If loading from a mod project, ensure all .buffer files are present alongside the .w2anims."
                )

        # Load from buffer file if it exists (cooked path)
        if os.path.exists(def_path):
            # Cooked animation - load from buffer file
            if (streamingOption is not None and streamingOption.Index.String == "ABSO_PartiallyStreamable"):
                # Partial streaming: combine inline data + buffer file
                f = open(def_path,"rb")
                def_data = f.read()
                data_in_file = chunk.GetVariableByName('data').value
                inline_size = len(data_in_file) if data_in_file else 0
                log.warning(f"[anim_buffer] ABSO_PartiallyStreamable: inline_data={inline_size}B, buffer={len(def_data)}B, path={def_path}")
                b = bytearray(data_in_file) + def_data
                the_data = bStream(data = b)
                f.close()
                source_detail = f"buffer_file+inline:{def_path}"
            else:
                # Full streaming: all data from buffer file
                f = open(def_path,"rb")
                b = f.read()
                the_data = bStream(data = b)
                f.close()
                source_detail = f"buffer_file:{def_path}"
        else:
            # For uncooked files the animation data lives in embedded CR2W buffers.
            # The deferredData.ValueA points to a 1-based buffer index
            buffer_index = deferredData.ValueA - 1  # Convert to 0-based index
            
            # Check scene toggle for prefer_uncompressed
            prefer_uncompressed = False
            try:
                import bpy
                prefer_uncompressed = getattr(bpy.context.scene, 'witcher_prefer_uncompressed_anims', False)
            except Exception:
                pass
            
            # If prefer_uncompressed is enabled and we have embedded animation data, use it
            if prefer_uncompressed and embedded_data and len(embedded_data) > 20:
                log.info(f"prefer_uncompressed enabled: using uncompressed animation data ({len(embedded_data)} bytes)")
                buffer = read_uncooked_anim_buffer(embedded_data, chunk, duration, Skeleton_file)
                buffer._source_detail = "embeddedAnimData:prefer_uncompressed"
                return buffer
            
            # Check if the CR2W file has embedded buffers
            if hasattr(file, 'BufferData') and file.BufferData and buffer_index >= 0 and buffer_index < len(file.BufferData):
                buffer_size = len(file.BufferData[buffer_index])
                log.info(f"Buffer file not found, checking embedded CR2W buffer #{buffer_index + 1} ({buffer_size} bytes)")

                # Check if bone dataAddr values exceed buffer size - if so, the buffer is incompatible
                max_data_addr = 0
                bones_prop = chunk.GetVariableByName('bones')
                if bones_prop is not None and hasattr(bones_prop, 'More'):
                    for bone in bones_prop.More:
                        for track_name in ['position', 'orientation', 'scale']:
                            track = getattr(bone, track_name, None)
                            if track:
                                da = track.GetVariableByName('dataAddr')
                                nf = track.GetVariableByName('numFrames')
                                if da:
                                    num_frames = nf.Value if nf else 1
                                    if track_name == 'orientation':
                                        bytes_per_frame = _bytes_per_quat(orientationCompressionMethod)
                                    else:
                                        comp_prop = track.GetVariableByName('compression')
                                        if comp_prop is not None:
                                            comp_val = comp_prop.Value
                                        else:
                                            comp_val = 2 if track_name == 'scale' else 0
                                        bytes_per_frame = _bytes_per_vec3(comp_val)
                                    bytes_needed = da.Value + (num_frames * bytes_per_frame)
                                    if bytes_needed > max_data_addr:
                                        max_data_addr = bytes_needed

                if max_data_addr > buffer_size:
                    log.warning(f"Buffer size ({buffer_size}) smaller than required ({max_data_addr}), dataAddr values exceed buffer bounds")
                    if embedded_data and len(embedded_data) > 0:
                        log.info(f"Falling back to uncompressed embedded data ({len(embedded_data)} bytes)")
                        buffer = read_uncooked_anim_buffer(embedded_data, chunk, duration, Skeleton_file)
                        buffer._source_detail = "embeddedAnimData:buffer_too_small"
                        return buffer
                    else:
                        log.warning("No embedded data available for fallback, animation may fail")

                # Use the embedded buffer with the standard cooked format (dataAddr offsets)
                the_data = bStream(data = bytearray(file.BufferData[buffer_index]))
                source_detail = f"embedded_buffer:{buffer_index + 1}"
            elif embedded_data and len(embedded_data) > 0:
                # Fallback: try the old embeddedAnimData from CSkeletalAnimation
                log.info(f"No embedded buffers, trying read_uncooked_anim_buffer for {len(embedded_data)} bytes")
                buffer = read_uncooked_anim_buffer(embedded_data, chunk, duration, Skeleton_file)
                buffer._source_detail = "embeddedAnimData:no_embedded_buffers"
                return buffer
            else:
                # No embedded buffers, no embedded data - try inline data as fallback
                data_prop = chunk.GetVariableByName('data')
                if data_prop is not None and hasattr(data_prop, 'value') and data_prop.value and len(data_prop.value) > 0:
                    the_data = bStream(data = bytearray(data_prop.value))
                    source_detail = "inline_data"
                elif the_fallback_data is None:
                    # No buffer file, no inline data, no fallback, no embedded data
                    log.warning("No animation data source found")
                    the_data = bStream(data = bytearray())
    else:
        # No deferred data - all animation data is inline
        data_prop = chunk.GetVariableByName('data')
        if data_prop is not None and hasattr(data_prop, 'value') and data_prop.value:
            the_data = bStream(data = bytearray(data_prop.value))
            source_detail = "inline_data"
        else:
            the_data = bStream(data = bytearray())
            source_detail = "inline_data:empty"
    # Determine which data source to use:
    # - For cooked animations: use the_data with dataAddr offsets
    # - If the_data is empty but fallbackData exists: use fallbackData with dataAddrFallback offsets
    use_fallback = False
    if the_data is None or (hasattr(the_data, 'data') and len(the_data.data) == 0):
        if the_fallback_data is not None:
            use_fallback = True
            source_detail = "fallback_data"
        else:
            the_data = bStream(data = bytearray())

    f = the_fallback_data if use_fallback else the_data
    bones_prop = chunk.GetVariableByName('bones')

    # Check if bones exist (they won't in uncooked files)
    if bones_prop is None or not hasattr(bones_prop, 'More') or len(bones_prop.More) == 0:
        log.warning('No bone data found in animation buffer')
        return w3_types.CAnimationBufferBitwiseCompressed([], [], duration=buffer_duration, numFrames=buffer_numFrames, dt=buffer_dt)

    for (idx, bone) in enumerate(bones_prop.More):
        if Skeleton_file and idx == Skeleton_file.nbBones:
            log.warning(f'Animation has more bone entiries than skeleton. Rig:{str(Skeleton_file.nbBones)}  Anim:{str(len(bones_prop.More))}')
            break
        this_bone = w2AnimsFrames(idx,
            BoneName = Skeleton_file.names[idx] if Skeleton_file else idx,
            position_dt = "",
            position_numFrames = "",
            positionFrames = [],
            rotation_dt = "",
            rotation_numFrames = "",
            rotationFrames = [],
            scale_dt = "",
            scale_numFrames = "",
            scaleFrames = [],
            rotationFramesQuat = "")
        #for item in boneData.More:
        this_bone.position_dt = bone.position.GetVariableByName('dt').Value
        this_bone.position_numFrames = bone.position.GetVariableByName('numFrames').Value
        compression = bone.position.GetVariableByName('compression')
        if compression is not None:
            compression = bone.position.GetVariableByName('compression').Value
        else:
            compression = 0
        dataAddr = bone.position.GetVariableByName('dataAddr')
        dataAddrFallback = bone.position.GetVariableByName('dataAddrFallback')
        if dataAddr is not None:
            dataAddr = dataAddr.Value
        else:
            dataAddr = 0
        if dataAddrFallback is not None:
            dataAddrFallback = dataAddrFallback.Value
        else:
            dataAddrFallback = 0
        # Use fallback address when in fallback mode
        actual_addr = dataAddrFallback if use_fallback else dataAddr
        f.seek(actual_addr)
        for _ in range(0, this_bone.position_numFrames):
            this_bone.positionFrames.append(CVector3D(f, compression).getList())
        # Diagnostic: log bones with multi-frame position data to spot decoding errors
        bone_label = Skeleton_file.names[idx] if Skeleton_file else idx
        if this_bone.position_numFrames > 1 and this_bone.positionFrames:
            first_pos = this_bone.positionFrames[0]
            max_abs = max(abs(v) for v in first_pos) if first_pos else 0
            if max_abs > 10.0:
                log.warning(f"[anim_buffer] bone[{idx}] '{bone_label}': LARGE position! dataAddr={actual_addr}, compression={compression}, numFrames={this_bone.position_numFrames}, first={[round(v,4) for v in first_pos]}")
            else:
                log.debug(f"[anim_buffer] bone[{idx}] '{bone_label}': dataAddr={actual_addr}, compression={compression}, numFrames={this_bone.position_numFrames}, first={[round(v,4) for v in first_pos]}")

        # if len(this_bone.positionFrames) == 0:
        #     this_bone.positionFrames = [{"x": 0.0,"y": 0.0,"z": 0.0}]
        this_bone.rotation_dt = bone.orientation.GetVariableByName('dt').Value
        this_bone.rotation_numFrames = bone.orientation.GetVariableByName('numFrames').Value
        dataAddr = bone.orientation.GetVariableByName('dataAddr')
        dataAddrFallback = bone.orientation.GetVariableByName('dataAddrFallback')
        compression = bone.orientation.GetVariableByName('compression')
        if compression is not None:
            compression = bone.orientation.GetVariableByName('compression').Value
        else:
            compression = 0
        if dataAddr is not None:
            dataAddr = dataAddr.Value
        else:
            dataAddr = 0
        if dataAddrFallback is not None:
            dataAddrFallback = dataAddrFallback.Value
        else:
            dataAddrFallback = 0
        # Use fallback address when in fallback mode
        actual_addr = dataAddrFallback if use_fallback else dataAddr
        f.seek(actual_addr)
        for _ in range(0, this_bone.rotation_numFrames):
            if "ABOCM_PackIn48bitsW" in orientationCompressionMethod:
                bits = ReadUlong48(f)
                orients = []
                orients.append((bits & 0x0000FFF000000000) >> 36)
                orients.append((bits & 0x0000000FFF000000) >> 24)
                orients.append((bits & 0x0000000000FFF000) >> 12)
                orients.append((bits & 0x0000000000000FFF))
                for (i, item) in enumerate(orients):
                    orients[i] = (2047.0 - orients[i]) * (1 / 2048.0)
                this_bone.rotationFrames.append(Quaternion(orients[0], orients[1], orients[2], orients[3]))
                #print(bits)
            elif "ABOCM_AsFloat_XYZSignedWInLastBit" in orientationCompressionMethod:
                (x, y, z) = CVector3D(f, compression).getList()
                int_values = [x for x in bytearray(struct.pack("f", z))]
                signW = (int_values[0] & 1) > 0
                minScalar = min(x * x + y * y + z * z, 1.0)
                w = math.sqrt(1.0 - minScalar)
                if signW:
                    w = -w
                this_bone.rotationFrames.append(Quaternion(x, y, z, w))
            elif "ABOCM_PackIn64bitsW" in orientationCompressionMethod:
                orients = []
                orients.append(readUShort(f))
                orients.append(readUShort(f))
                orients.append(readUShort(f))
                orients.append(readUShort(f))

                for (i, item) in enumerate(orients):
                    orients[i] = (32768.0 - orients[i]) * (1 / 32767.0)
                this_bone.rotationFrames.append(Quaternion(orients[0], orients[1], orients[2], orients[3]))
            elif "ABOCM_PackIn40bitsW" in orientationCompressionMethod:
                bits = ReadUlong40(f)
                orients = []
                orients.append((bits >> 30) & 0b1111111111)
                orients.append((bits >> 20) & 0b1111111111)
                orients.append((bits >> 10) & 0b1111111111)
                orients.append(bits & 0b1111111111)
                for (i, item) in enumerate(orients):
                    orients[i] = (511.0 - orients[i]) * (1 / 512.0)
                this_bone.rotationFrames.append(Quaternion(orients[0], orients[1], orients[2], orients[3]))
            else:
                log.error('UNDEFINED orientationCompressionMethod FOUND')
                #raise Exception('UNDEFINED orientationCompressionMethod FOUND')
        this_bone.scale_dt = bone.scale.GetVariableByName('dt').Value
        this_bone.scale_numFrames = bone.scale.GetVariableByName('numFrames').Value
        compression = bone.scale.GetVariableByName('compression')
        if compression is not None:
            compression = bone.scale.GetVariableByName('compression').Value
        else:
            compression = 2
        dataAddr = bone.scale.GetVariableByName('dataAddr')
        dataAddrFallback = bone.scale.GetVariableByName('dataAddrFallback')
        if dataAddr is not None:
            dataAddr = dataAddr.Value
        else:
            dataAddr = 0
        if dataAddrFallback is not None:
            dataAddrFallback = dataAddrFallback.Value
        else:
            dataAddrFallback = 0
        # Use fallback address when in fallback mode
        actual_addr = dataAddrFallback if use_fallback else dataAddr
        f.seek(actual_addr)
        for _ in range(0, this_bone.scale_numFrames):
            this_bone.scaleFrames.append(CVector3D(f, compression).getList())
        this_bone.rotationFramesQuat = this_bone.rotationFrames
        bones.append(this_bone)
    tracks_prop = chunk.GetVariableByName('tracks')
    if tracks_prop is not None and hasattr(tracks_prop, 'More'):
        for (idx, track) in enumerate(tracks_prop.More):
            this_track = Track(idx,
                trackName = _safe_track_name(Skeleton_file, idx),
                numFrames = "",
                dt = "",
                trackFrames = [])
            trackData = track
            this_track.dt = trackData.GetVariableByName('dt').Value
            this_track.numFrames = trackData.GetVariableByName('numFrames').Value
            compression = trackData.GetVariableByName('compression')
            if compression is not None:
                compression = compression.Value
            else:
                compression = 0
            dataAddr = trackData.GetVariableByName('dataAddr')
            dataAddrFallback = trackData.GetVariableByName('dataAddrFallback')
            if dataAddr is not None:
                dataAddr = dataAddr.Value
            else:
                dataAddr = 0
            if dataAddrFallback is not None:
                dataAddrFallback = dataAddrFallback.Value
            else:
                dataAddrFallback = 0
            # Use fallback address when in fallback mode
            actual_addr = dataAddrFallback if use_fallback else dataAddr
            f.seek(actual_addr)
            for _ in range(0, this_track.numFrames):
                this_track.trackFrames.append(ReadCompressFloat(f, compression).val)
            tracks.append(this_track)
    buffer = w3_types.CAnimationBufferBitwiseCompressed(bones, tracks, duration=buffer_duration, numFrames=buffer_numFrames, dt=buffer_dt)
    buffer._source = source_kind
    buffer._source_detail = source_detail
    return buffer

def create_anim(file, CSkeletalAnimation, CAnimationBuffer, Skeleton_file):
    chunk = CSkeletalAnimation
    SkeletalAnimationType = "SAT_Normal"
    AdditiveType = None
    MotionExtraction = None
    UncompressedMotionExtraction = None

    for prop in chunk.PROPS:
        if prop.theName == "name":
            name = prop.Index.String
        elif prop.theName == "duration":
            duration = prop.Value
        elif prop.theName == "framesPerSecond":
            framesPerSecond = prop.Value
        elif prop.theName == "Animation type for reimport":
            SkeletalAnimationType = prop.ToString()
        elif prop.theName == "Additive type for reimport":
            AdditiveType = prop.ToString()
        elif prop.theName == "motionExtraction":
            motion_ptr = CSkeletalAnimation.GetVariableByName('motionExtraction')
            log.debug(f'motionExtraction pointer value: {motion_ptr.Value if motion_ptr else None}')
            anim_motionExtraction = file.CHUNKS.CHUNKS[motion_ptr.Value-1] if motion_ptr and motion_ptr.Value else None
            if anim_motionExtraction:
                frames_prop = anim_motionExtraction.GetVariableByName('frames')
                deltaTimes_prop = anim_motionExtraction.GetVariableByName('deltaTimes')
                flags_prop = anim_motionExtraction.GetVariableByName('flags')
                log.debug(f'Motion extraction chunk type: {anim_motionExtraction.name}')
                log.debug(f'  frames prop: {frames_prop}, value: {frames_prop.value if frames_prop and hasattr(frames_prop, "value") else "N/A"} (count: {len(frames_prop.value) if frames_prop and hasattr(frames_prop, "value") and frames_prop.value else 0})')
                log.debug(f'  deltaTimes prop: {deltaTimes_prop}, More: {deltaTimes_prop.More if deltaTimes_prop and hasattr(deltaTimes_prop, "More") else "N/A"}')
                log.debug(f'  flags prop: {flags_prop}, Value: {flags_prop.Value if flags_prop and hasattr(flags_prop, "Value") else "N/A"}')
                MotionExtraction = {
                    "duration": chunk.GetVariableByName('duration').Value,
                    "frames": frames_prop.value if frames_prop and hasattr(frames_prop, 'value') else [],
                    "deltaTimes": deltaTimes_prop.More if deltaTimes_prop and hasattr(deltaTimes_prop, 'More') else [],
                    "flags": flags_prop.Value if flags_prop and hasattr(flags_prop, 'Value') else 0,
                }
                log.info(f'Loaded motion extraction: {len(MotionExtraction["frames"])} frame values, {len(MotionExtraction["deltaTimes"])} delta times, flags={MotionExtraction["flags"]}')
        elif prop.theName == "uncompressedMotionExtraction":
            # For uncooked animations - check if uncompressed motion extraction exists
            uncompressed_ptr = CSkeletalAnimation.GetVariableByName('uncompressedMotionExtraction')
            if uncompressed_ptr and uncompressed_ptr.Value:
                chunk_idx = uncompressed_ptr.Value - 1
                if 0 <= chunk_idx < len(file.CHUNKS.CHUNKS):
                    uncompressed_chunk = file.CHUNKS.CHUNKS[chunk_idx]
                    log.debug(f'Found uncompressedMotionExtraction: {uncompressed_chunk.name}')
                    UncompressedMotionExtraction = uncompressed_chunk
                else:
                    log.debug(f'uncompressedMotionExtraction pointer {uncompressed_ptr.Value} out of range')

    # Check if CSkeletalAnimation has embedded animation data (uncooked files)
    embedded_data = getattr(CSkeletalAnimation, 'embeddedAnimData', None)
    if embedded_data and len(embedded_data) > 0:
        log.info(f'Found {len(embedded_data)} bytes of embedded animation data in CSkeletalAnimation')

    if CAnimationBuffer.Type == "CAnimationBufferMultipart":
        parts = CAnimationBuffer.GetVariableByName('parts')
        BufferMultipart = w3_types.CAnimationBufferMultipart(numFrames=CAnimationBuffer.GetVariableByName('numFrames').Value,
                                                             numBones=CAnimationBuffer.GetVariableByName('numBones').Value,
                                                             numTracks=CAnimationBuffer.GetVariableByName('numTracks').Value if CAnimationBuffer.GetVariableByName('numTracks') else 0,
                                                             firstFrames=CAnimationBuffer.GetVariableByName('firstFrames').value,
                                                             parts=[])
        parts_done = []
        for part in parts.value:
            buffer = read_anim_buffer(file, file.CHUNKS.CHUNKS[part-1], duration, Skeleton_file)
            parts_done.append(buffer)
        BufferMultipart.parts = parts_done
        buffer = BufferMultipart

    else:
        # Try to read from animation buffer, passing embedded data for uncooked files
        buffer = read_anim_buffer(file, CAnimationBuffer, duration, Skeleton_file, embedded_data)

    anim = w3_types.CSkeletalAnimation(name, duration, framesPerSecond, animBuffer=buffer, SkeletalAnimationType = SkeletalAnimationType, AdditiveType = AdditiveType, motionExtraction=MotionExtraction)
    if UncompressedMotionExtraction is not None:
        anim.uncompressedMotionExtraction = UncompressedMotionExtraction
    return anim

def create_anim_set(file, Skeleton_file):
    CHUNKS = file.CHUNKS.CHUNKS
    for chunk in CHUNKS:
        if chunk.name == "CSkeletalAnimationSet" or chunk.name == "CCutsceneTemplate":
            set = chunk
            break;
    skeleton = set.GetVariableByName('skeleton')
    set_animations = set.GetVariableByName('animations')
    animations = []
    for idx, anim_ptr in enumerate(set_animations.value):
        anim_entry = CHUNKS[anim_ptr-1]
        anim = CHUNKS[anim_entry.GetVariableByName('animation').Value-1]
        anim_buffer = CHUNKS[anim.GetVariableByName('animBuffer').Value-1]
        log.info(str(idx)+" "+anim.GetVariableByName('name').Index.String)
        animation = create_anim(file, anim, anim_buffer, Skeleton_file)
        entries = []
        final_entry = w3_types.CSkeletalAnimationSetEntry(animation, entries)
        animations.append(final_entry)

    final_set = w3_types.CSkeletalAnimationSet(animations)
    return final_set

def load_lipsync_file(fileName_in = False) -> w3_types.CSkeletalAnimation:
    if fileName_in:
        fileName = fileName_in
    face_fileName = repo_file(r"characters\models\geralt\head\model\h_01_mg__geralt.w3fac")
    with open(face_fileName,"rb") as f:
        theFile = getCR2W(f)
        f.close()
        CMimicFace = create_CMimicFace(theFile)
        
    def check_magic_number(filepath, magic_string):
        try:
            with open(filepath, 'rb') as file:
                return file.read(len(magic_string)) == magic_string.encode()
        except IOError:
            return False
    if check_magic_number(fileName, "CR2W"):
        with open(fileName,"rb") as f:
            theFile = getCR2W(f, anim_name = False)
        anim = create_lipsync_anim(theFile, CMimicFace.floatTrackSkeleton)
    else:
        anim = read_lipsync_buffer_file(fileName, CMimicFace.floatTrackSkeleton)

    return anim

def load_base_skeleton(rigPath):
    with open(rigPath, "rb") as f:
        theFile = getCR2W(f)
        f.close()
        if rigPath.endswith('.w3fac'):
            CMimicFace = create_CMimicFace(theFile)
            return CMimicFace.floatTrackSkeleton
        elif rigPath.endswith('.w2rig'):
            return create_Skeleton(theFile)
        else:
            log.error('Error loading rig, check path and extension.')
            return None

def load_bin_anims_info(fileName, anim_name = None, rigPath = None) -> w3_types.CSkeletalAnimationSet:
    if not rigPath:
        rigPath = repo_file(r"characters\models\geralt\head\model\h_01_mg__geralt.w3fac")
    
    rig = load_base_skeleton(rigPath)
    with open(fileName, "rb") as f:
        theFile = getCR2W(f, "cake")
    anim_set = create_anim_set(theFile, rig)
    return anim_set


def _is_w2_cr2w_version_file(file_name):
    try:
        with open(file_name, "rb") as f:
            if f.read(4) != b"CR2W":
                return False
            version = struct.unpack("<I", f.read(4))[0]
            return version <= 115
    except Exception:
        return False


def load_bin_anims_single(fileName, anim_name = None, rigPath = None) -> w3_types.CSkeletalAnimationSet:
    if _is_w2_cr2w_version_file(fileName):
        return load_w2_anims_full(fileName, rigPath=rigPath, anim_name=anim_name)

    if not rigPath:
        rigPath = repo_file(r"characters\models\geralt\head\model\h_01_mg__geralt.w3fac")
    
    rig = load_base_skeleton(rigPath)
    
    repo_file(fileName, is_abs_path = True) # Make sure anims file exists on disk
    with open(fileName, "rb") as f:
        theFile = getCR2W(f, anim_name)
        f.close()
    anim_set = create_anim_set(theFile, rig)
    return anim_set
    
def load_bin_anims(fileName, rigPath = False) -> w3_types.CSkeletalAnimationSet:
    
    rig = False
    # if not rigPath:
    #     rigPath = repo_file(r"characters\base_entities\man_base\man_base.w2rig")
    #     if "witcher_scabbards" in fileName:
    #         rigPath = repo_file(r"characters\models\geralt\scabbards\model\scabbards_crossbow.w2rig")
    if rigPath:
        rig = load_base_skeleton(rigPath)
    #LOAD THE BASE SKELETON

    with open(fileName, "rb") as f:
        theFile = getCR2W(f)
        f.close()
    anim_set = create_anim_set(theFile, rig)
    return anim_set


def create_anim_set_info_only(file, havok_infos=None, quiet=False):
    """Create a CSkeletalAnimationSet with metadata only (no buffer decoding).

    This is used for W2 .w2anims files where the animation buffers contain
    Havok data that we cannot decode yet. We extract only the listing info:
    name, duration, framesPerSecond, numFrames, SkeletalAnimationType.
    """
    CHUNKS = file.CHUNKS.CHUNKS
    set_chunk = None
    for chunk in CHUNKS:
        if chunk.name == "CSkeletalAnimationSet":
            set_chunk = chunk
            break
    if set_chunk is None:
        log.error("No CSkeletalAnimationSet chunk found")
        return w3_types.CSkeletalAnimationSet([])

    set_animations = set_chunk.GetVariableByName('animations')
    if set_animations is None:
        log.warning("CSkeletalAnimationSet has no animations property")
        return w3_types.CSkeletalAnimationSet([])

    # W2 CR2W stores handle arrays as Handles (list of HANDLE objects),
    # W3 CR2W stores them as value (list of ints). Resolve to chunk indices.
    anim_ptrs = []
    if hasattr(set_animations, 'Handles') and set_animations.Handles:
        for h in set_animations.Handles:
            if h.ChunkHandle and h.val:
                anim_ptrs.append(h.val)  # 1-based chunk index
    elif hasattr(set_animations, 'value') and set_animations.value:
        anim_ptrs = set_animations.value  # already 1-based chunk indices

    if not anim_ptrs:
        log.warning("CSkeletalAnimationSet animations list is empty")
        return w3_types.CSkeletalAnimationSet([])

    animations = []
    for idx, anim_ptr in enumerate(anim_ptrs):
        try:
            anim_entry = CHUNKS[anim_ptr - 1]
            # The 'animation' property is a single handle — get its Value
            anim_prop = anim_entry.GetVariableByName('animation')
            if anim_prop is None:
                log.warning("Entry %d has no 'animation' property", idx)
                continue
            # Single handle: .Value is set for both W2/W3
            anim_chunk_idx = anim_prop.Value
            if not anim_chunk_idx:
                # Try via Handles list
                if hasattr(anim_prop, 'Handles') and anim_prop.Handles:
                    anim_chunk_idx = anim_prop.Handles[0].val
            if not anim_chunk_idx:
                log.warning("Entry %d: could not resolve animation chunk", idx)
                continue
            anim_chunk = CHUNKS[anim_chunk_idx - 1]

            # Extract metadata from CSkeletalAnimation properties
            name = "unknown"
            duration = 0.0
            framesPerSecond = 30.0
            numFrames = 0
            SkeletalAnimationType = "SAT_Normal"
            AdditiveType = None
            has_motion = False
            anim_times = None

            # Log available property names on first entry for debugging
            if idx == 0:
                entry_props = [p.theName for p in anim_entry.PROPS] if hasattr(anim_entry, 'PROPS') else []
                anim_props = [p.theName for p in anim_chunk.PROPS] if hasattr(anim_chunk, 'PROPS') else []
                log.debug("Entry props: %s", entry_props)
                log.debug("Anim props: %s", anim_props)

            for prop in anim_chunk.PROPS:
                if prop.theName == "name":
                    name = prop.Index.String
                elif prop.theName == "duration":
                    duration = prop.Value
                elif prop.theName == "framesPerSecond":
                    framesPerSecond = prop.Value
                elif prop.theName == "animTimes":
                    # W2: array of floats, typically [duration, offset] or similar
                    if hasattr(prop, 'value') and prop.value:
                        anim_times = prop.value
                elif prop.theName == "Animation type for reimport":
                    SkeletalAnimationType = prop.ToString()
                elif prop.theName == "Additive type for reimport":
                    AdditiveType = prop.ToString()
                elif prop.theName == "motionExtraction":
                    has_motion = True

            # Also check the entry chunk for duration/fps (some W2 versions)
            if hasattr(anim_entry, 'PROPS'):
                for prop in anim_entry.PROPS:
                    if prop.theName == "duration" and duration == 0.0:
                        duration = prop.Value
                    elif prop.theName == "framesPerSecond" and framesPerSecond == 30.0:
                        framesPerSecond = prop.Value
                    elif prop.theName == "numFrames" and numFrames == 0:
                        numFrames = prop.Value

            # Use Havok blob data if available (W2 path)
            if havok_infos and idx < len(havok_infos):
                hk_info = havok_infos[idx]
                if hk_info.duration > 0.0:
                    duration = hk_info.duration
                if hk_info.num_frames > 0:
                    numFrames = hk_info.num_frames
                if hk_info.num_transform_tracks > 0 and numFrames == 0:
                    # Estimate frames from duration and default fps
                    numFrames = max(1, int(duration * framesPerSecond))

            # W2 fallback: try extracting duration from animTimes
            import math
            if duration == 0.0 and anim_times:
                for val in anim_times:
                    if val and not math.isnan(val) and val > 0.0:
                        duration = val
                        break

            # W3 path: get numFrames from the animation buffer chunk
            if numFrames == 0:
                anim_buffer_ptr = anim_chunk.GetVariableByName('animBuffer')
                if anim_buffer_ptr:
                    buf_val = getattr(anim_buffer_ptr, 'Value', None)
                    if not buf_val and hasattr(anim_buffer_ptr, 'Handles') and anim_buffer_ptr.Handles:
                        buf_val = anim_buffer_ptr.Handles[0].val
                    if buf_val:
                        try:
                            buffer_chunk = CHUNKS[buf_val - 1]
                            nf = buffer_chunk.GetVariableByName('numFrames')
                            if nf:
                                numFrames = nf.Value
                            if duration == 0.0:
                                dur = buffer_chunk.GetVariableByName('duration')
                                if dur:
                                    duration = dur.Value
                        except (IndexError, AttributeError):
                            pass
            if numFrames == 0 and duration > 0 and framesPerSecond > 0:
                numFrames = max(1, int(duration * framesPerSecond))

            # Create a stub animation buffer with just the frame count
            stub_buffer = w3_types.CAnimationBufferBitwiseCompressed(
                [], [], duration=duration,
                numFrames=numFrames, dt=(duration / max(numFrames - 1, 1)) if numFrames > 1 else 0.0
            )

            anim = w3_types.CSkeletalAnimation(
                name, duration, framesPerSecond,
                animBuffer=stub_buffer,
                SkeletalAnimationType=SkeletalAnimationType,
                AdditiveType=AdditiveType,
                motionExtraction={'duration': duration, 'frames': [], 'deltaTimes': [], 'flags': 0} if has_motion else None
            )

            final_entry = w3_types.CSkeletalAnimationSetEntry(anim, [])
            animations.append(final_entry)
            log.debug("%d %s (%.2fs, %d frames)", idx, name, duration, numFrames)
        except Exception as e:
            log.warning("Failed to read animation entry %d: %s", idx, e)
            continue

    if not quiet:
        log.info("Loaded %d animation entries (info only)", len(animations))
    return w3_types.CSkeletalAnimationSet(animations)


def load_w2_anims_info(fileName) -> w3_types.CSkeletalAnimationSet:
    """Load a W2 .w2anims file and return animation set metadata only.

    W2 .w2anims files are CR2W containers with embedded Havok animation
    buffers. This function scans the embedded Havok blobs to extract
    duration and frame counts, and merges with CR2W animation names.
    """
    # Read raw bytes for Havok blob scanning
    with open(fileName, "rb") as f:
        raw_data = f.read()

    # Scan all embedded Havok packfile blobs for animation metadata
    havok_infos = HavokPackfile.scan_animation_blobs(raw_data)
    log.info("Scanned %d Havok animation blobs from %s",
             len(havok_infos), os.path.basename(fileName))

    # Parse CR2W structure for animation names
    with open(fileName, "rb") as f:
        theFile = getCR2W(f)

    return create_anim_set_info_only(theFile, havok_infos=havok_infos)


def _get_fallback_w2_bone_names(rigPath=None):
    if not rigPath:
        return None
    try:
        rig = load_base_skeleton(rigPath)
    except Exception as exc:
        log.warning("Failed to load fallback rig for W2 bone mapping: %s", exc)
        return None
    names = getattr(rig, 'names', None)
    if names:
        return names
    return None


def _apply_decoded_animation_entry(entry, decoded):
    if not entry or not entry.animation or not decoded or not getattr(decoded, 'buffer', None):
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


def load_w2_anims_full(fileName, rigPath=None, anim_name=None) -> w3_types.CSkeletalAnimationSet:
    """Load a W2 .w2anims file with full Havok spline decompression."""
    with open(fileName, "rb") as f:
        raw_data = f.read()

    fallback_bone_names = _get_fallback_w2_bone_names(rigPath)
    with open(fileName, "rb") as f:
        theFile = getCR2W(f)

    anim_set = create_anim_set_info_only(theFile, quiet=bool(anim_name))

    if anim_name:
        target_idx = None
        for idx, entry in enumerate(anim_set.animations):
            if entry.animation and entry.animation.name == anim_name:
                target_idx = idx
                break

        if target_idx is None:
            log.warning("Animation '%s' not found in %s", anim_name, os.path.basename(fileName))
            anim_set.animations = []
            return anim_set

        decoded = HavokPackfile.decode_animation_blob_at_index(
            raw_data,
            target_idx,
            fallback_bone_names=fallback_bone_names,
        )
        if decoded and getattr(decoded, 'buffer', None):
            _apply_decoded_animation_entry(anim_set.animations[target_idx], decoded)
            log.info(
                "Decoded W2 animation '%s' (blob %d/%d) from %s",
                anim_name,
                target_idx + 1,
                len(anim_set.animations),
                os.path.basename(fileName),
            )
        else:
            log.warning(
                "Failed to decode W2 animation '%s' (blob index %d) from %s",
                anim_name,
                target_idx,
                os.path.basename(fileName),
            )

        anim_set.animations = [anim_set.animations[target_idx]]
        return anim_set

    decoded_infos = HavokPackfile.scan_and_decode_animations(
        raw_data, fallback_bone_names=fallback_bone_names
    )
    log.info("Decoded %d Havok animation blobs from %s",
             len(decoded_infos), os.path.basename(fileName))

    if len(decoded_infos) != len(anim_set.animations):
        log.warning(
            "W2 blob count (%d) does not match animation entries (%d)",
            len(decoded_infos),
            len(anim_set.animations),
        )

    for idx, entry in enumerate(anim_set.animations):
        if idx >= len(decoded_infos):
            continue
        _apply_decoded_animation_entry(entry, decoded_infos[idx])

    return anim_set


def create_CCutscene(file):
    CHUNKS = file.CHUNKS.CHUNKS
    for chunk in CHUNKS:
        if chunk.name == "CCutsceneTemplate":
            set = chunk
            break;
    set_animations = set.GetVariableByName('animations')
    actorsDef = set.GetVariableByName('actorsDef')
    actors = []
    actorsdict = {}
    for actor in actorsDef.More:
        ActorDef = w3_types.SCutsceneActorDef(False, actor)
        actors.append(ActorDef)
        actorsdict[ActorDef.name] = ActorDef
    animations = []
    for idx, anim_ptr in enumerate(set_animations.value):
        anim_entry = CHUNKS[anim_ptr-1]
        anim = CHUNKS[anim_entry.GetVariableByName('animation').Value-1]
        anim_name = anim.GetVariableByName('name').Index.String
        (act, comp, anim_n) = anim_name.split(':')
        
        chosen_actor =actorsdict[act]
        
        ##
        #characters\\base_entities\\man_base\\man_base.w2rig
        #geralt w2fac
        #loop imports of entity and sub entity and find the first w2rig and w2fac
        #TODO make a quick read function that can lookup skeleton
        #filepath = repo_file(chosen_actor.template)
        
        # def getskelly(filepath, skeleton, face):
        #     with open(filepath, "rb") as f:
        #         theFile = getCR2W(f, do_read_chunks = False)
        #         if hasattr(theFile, 'CR2WImport'):
        #             for imp in theFile.CR2WImport:
        #                 if not skeleton:
        #                     skeleton = imp.path if imp.path.endswith('.w2rig') else None
        #                 if not face:
        #                     face = imp.path if imp.path.endswith('.w2fac') else None
        #             if skeleton and face:
        #                 f.close()
        #                 return skeleton, face

        #             for imp in theFile.CR2WImport:
        #                 if imp.path.endswith('.w2ent'):
        #                     skeleton, face = getskelly(repo_file(imp.path), skeleton, face)
        #                 if imp.path.endswith('.w2mesh'):
        #                     skeleton, face = getskelly(repo_file(imp.path), skeleton, face)
                    
        #         f.close()
        #         return skeleton, face 
        
        # skeleton, face = None, None
        # skeleton, face = getskelly(filepath, skeleton, face)
        
        anim_buffer = CHUNKS[anim.GetVariableByName('animBuffer').Value-1]
        log.info(str(idx)+" "+anim.GetVariableByName('name').Index.String)
        animation = create_anim(file, anim, anim_buffer, None)
        entries = []
        final_entry = w3_types.CSkeletalAnimationSetEntry(animation, entries)
        animations.append(final_entry)

    final_set = w3_types.CCutsceneTemplate(animations = animations, SCutsceneActorDefs = actors)
    return final_set

def load_bin_cutscene(fileName) -> w3_types.CCutsceneTemplate:
    with open(fileName, "rb") as f:
        theFile = getCR2W(f)
        f.close()
    anim_set = create_CCutscene(theFile)
    return anim_set
