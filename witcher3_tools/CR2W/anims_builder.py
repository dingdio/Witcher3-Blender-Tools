import io
import struct
from typing import List, Dict, Tuple, Optional

from .CR2W_helpers import Enums
from .CR2W_types import (
    CR2W,
    CR2W_header,
    CR2WBuffer,
    CR2WExport,
    CR2WProperty,
    DATA,
    W_CLASS,
    PROPERTY,
    HANDLE,
    CEnum,
    CSTRING,
    CDATETIME,
)

DEFAULT_DT = 0.0333333351
DEFAULT_VERSION = 3
DEFAULT_UNK = 94
DEFAULT_FLAGS = 0x0101
DEFAULT_DURATION_FACTOR = 1.0

DEFAULT_HEADER_VERSION = 163
DEFAULT_BUILD_VERSION = 9908608


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _float_equal(a: float, b: float, eps: float = 1e-6) -> bool:
    return abs(a - b) <= eps


def _component_is_constant(values: List[float], eps: float = 1e-6) -> bool:
    if not values:
        return True
    first = values[0]
    for v in values[1:]:
        if not _float_equal(v, first, eps):
            return False
    return True


def _pad_frames(frames: List[Tuple[float, ...]], num_frames: int) -> List[Tuple[float, ...]]:
    if not frames:
        return [(0.0,)] * max(num_frames, 0)
    if len(frames) == num_frames:
        return frames
    if len(frames) > num_frames:
        return frames[:num_frames]
    pad = [frames[-1]] * (num_frames - len(frames))
    return frames + pad


def _track_is_multi(frames: List[Tuple[float, ...]], components: int, eps: float = 1e-6) -> bool:
    if len(frames) <= 1:
        return False
    for comp_idx in range(components):
        values = [frame[comp_idx] for frame in frames]
        if not _component_is_constant(values, eps):
            return True
    return False


def _write_float16(value: float) -> bytes:
    """Write a float as truncated Float16 (top 2 bytes of float32)."""
    return struct.pack('<f', float(value))[2:]


def _write_float32_vec3(x: float, y: float, z: float) -> bytes:
    """Write 3 Float32 values (12 bytes)."""
    return struct.pack('<fff', float(x), float(y), float(z))


def _write_quat_raw(x: float, y: float, z: float, w: float) -> bytes:
    """Pack quaternion as ABOCM_AsFloat_XYZSignedWInLastBit.

    Stores X, Y, Z as Float32. W sign is encoded in the least significant
    bit of Z's float32 representation. W magnitude is reconstructed from
    the unit quaternion constraint: w = sqrt(1 - x^2 - y^2 - z^2).
    """
    x_bytes = struct.pack('<f', float(x))
    y_bytes = struct.pack('<f', float(y))
    z_bytes = bytearray(struct.pack('<f', float(z)))
    # Encode the sign of W in the least significant bit of Z's float32 bytes.
    if w < 0:
        z_bytes[0] |= 1   # set LSB = 1 when original W is negative
    else:
        z_bytes[0] &= ~1  # clear LSB = 0 when original W is non-negative
    return x_bytes + y_bytes + bytes(z_bytes)


def _write_quat_48bit(x: float, y: float, z: float, w: float) -> bytes:
    """Pack quaternion into 48-bit format (ABOCM_PackIn48bitsW).

    Reader decodes: component = (2047.0 - raw_12bit) / 2048.0.
    """
    def _enc(val):
        raw = round(2047.0 - val * 2048.0)
        return max(0, min(4095, raw))

    raw_x = _enc(x)
    raw_y = _enc(y)
    raw_z = _enc(z)
    raw_w = _enc(w)

    bits = (raw_x << 36) | (raw_y << 24) | (raw_z << 12) | raw_w
    return bytes([
        (bits >> 40) & 0xFF, (bits >> 32) & 0xFF,
        (bits >> 24) & 0xFF, (bits >> 16) & 0xFF,
        (bits >> 8) & 0xFF, bits & 0xFF,
    ])


def build_cooked_anim_buffer(
    bones: List[Dict[str, List[Tuple[float, ...]]]],
    num_frames: int,
    use_raw: bool = True,
) -> Tuple[bytes, bytes, List[Dict]]:
    """Build cooked animation buffer data with correct dataAddr offsets.

    When use_raw=True (default): ABBCP_Raw format
        - Positions: Float16 (compression=2, 6 bytes/frame)
        - Rotations: AsFloat_XYZSignedWInLastBit (12 bytes/frame)
        - Scales: Float16 (compression=2, 6 bytes/frame)

    When use_raw=False: ABBCP_NormalQuality format
        - Positions: Float16 (compression=2, 6 bytes/frame)
        - Rotations: PackIn48bitsW (6 bytes/frame)
        - Scales: Float16 (compression=2, 6 bytes/frame)

    Returns:
        (buffer_bytes, fallback_bytes, bone_info) where bone_info is a list
        of dicts with 'pos_addr', 'rot_addr', 'scale_addr',
        'pos_addr_fb', 'rot_addr_fb', 'scale_addr_fb',
        'pos_multi', 'rot_multi', 'scale_multi' per bone.
    """
    buf = io.BytesIO()       # main buffer (all frames)
    fb_buf = io.BytesIO()    # fallback buffer (first frame only)
    bone_info = []

    pos_bytes_per_frame = 6   # always Float16 (compression=2)
    rot_bytes_per_frame = 12 if use_raw else 6
    scale_bytes_per_frame = 6  # always Float16

    for bone in bones:
        pos_frames = _pad_frames(bone["pos_frames"], num_frames)
        rot_frames = _pad_frames(bone["rot_frames"], num_frames)
        scale_frames = _pad_frames(bone["scale_frames"], num_frames)

        pos_multi = _track_is_multi(pos_frames, 3)
        rot_multi = _track_is_multi(rot_frames, 4)
        scale_multi = _track_is_multi(scale_frames, 3)

        info = {
            'pos_multi': pos_multi,
            'rot_multi': rot_multi,
            'scale_multi': scale_multi,
        }

        # -- Position (always Float16, compression=2) --
        info['pos_addr'] = buf.tell()
        info['pos_addr_fb'] = fb_buf.tell()
        n_pos = num_frames if pos_multi else 1
        for i in range(n_pos):
            buf.write(_write_float16(pos_frames[i][0]))
            buf.write(_write_float16(pos_frames[i][1]))
            buf.write(_write_float16(pos_frames[i][2]))
        # Fallback: always first frame only
        fb_buf.write(_write_float16(pos_frames[0][0]))
        fb_buf.write(_write_float16(pos_frames[0][1]))
        fb_buf.write(_write_float16(pos_frames[0][2]))

        # -- Rotation --
        info['rot_addr'] = buf.tell()
        info['rot_addr_fb'] = fb_buf.tell()
        n_rot = num_frames if rot_multi else 1
        for i in range(n_rot):
            if use_raw:
                buf.write(_write_quat_raw(rot_frames[i][0], rot_frames[i][1], rot_frames[i][2], rot_frames[i][3]))
            else:
                buf.write(_write_quat_48bit(rot_frames[i][0], rot_frames[i][1], rot_frames[i][2], rot_frames[i][3]))
        # Fallback: first frame only
        if use_raw:
            fb_buf.write(_write_quat_raw(rot_frames[0][0], rot_frames[0][1], rot_frames[0][2], rot_frames[0][3]))
        else:
            fb_buf.write(_write_quat_48bit(rot_frames[0][0], rot_frames[0][1], rot_frames[0][2], rot_frames[0][3]))

        # -- Scale --
        info['scale_addr'] = buf.tell()
        info['scale_addr_fb'] = fb_buf.tell()
        n_scl = num_frames if scale_multi else 1
        for i in range(n_scl):
            buf.write(_write_float16(scale_frames[i][0]))
            buf.write(_write_float16(scale_frames[i][1]))
            buf.write(_write_float16(scale_frames[i][2]))
        # Fallback: first frame only
        fb_buf.write(_write_float16(scale_frames[0][0]))
        fb_buf.write(_write_float16(scale_frames[0][1]))
        fb_buf.write(_write_float16(scale_frames[0][2]))

        bone_info.append(info)

    return buf.getvalue(), fb_buf.getvalue(), bone_info


def _make_vector_prop(name: str, x: float, y: float, z: float, w: float = 1.0) -> PROPERTY:
    return PROPERTY(theName=name, theType="Vector", More=[
        PROPERTY(Value=float(x), theName="X", theType="Float"),
        PROPERTY(Value=float(y), theName="Y", theType="Float"),
        PROPERTY(Value=float(z), theName="Z", theType="Float"),
        PROPERTY(Value=float(w), theName="W", theType="Float"),
    ])


# ---------------------------------------------------------------------------
# CR2W construction helpers (module-level, take cr2w as parameter)
# ---------------------------------------------------------------------------

def _init_cr2w(header_version: int = DEFAULT_HEADER_VERSION,
               build_version: int = DEFAULT_BUILD_VERSION) -> CR2W:
    cr2w = CR2W()
    cr2w.CNAMES = []
    cr2w.HEADER = CR2W_header(
        CRC32=0, bufferSize=0, buildVersion=build_version,
        fileSize=0, flags=0, magic=1462915651,
        numChunks=0, timestamp=0, version=header_version,
    )
    cr2w.HEADER.timestamp = 0
    cr2w.CR2WImport = []
    cr2w.CR2W_Property = [CR2WProperty()]
    cr2w.CR2WBuffer = []
    cr2w.BufferData = []
    cr2w.CR2WExport = []
    cr2w.CHUNKS = DATA()
    return cr2w


def _add_chunk(cr2w: CR2W, chunk_type: str, props: List[PROPERTY]):
    idx = cr2w.HEADER.numChunks
    cr2w.HEADER.numChunks += 1
    cr2w.CR2WExport.append(
        CR2WExport(
            crc32=0, dataOffset=0, dataSize=0, name=chunk_type,
            objectFlags=0, parentID=0, template=0,
        )
    )
    chunk = W_CLASS(CR2WFILE=cr2w, idx=idx, PROPS=props, Type=chunk_type, name=chunk_type)
    cr2w.CHUNKS.CHUNKS.append(chunk)
    return idx, chunk


def _make_enum_prop(cr2w: CR2W, prop_name: str, enum_type: str, value: str) -> PROPERTY:
    enum_obj = CEnum(cr2w)
    enum_obj.String = value
    enum_obj.strings = [value]
    return PROPERTY(theName=prop_name, theType=enum_type, Index=enum_obj)


def _make_handle(cr2w: CR2W, ref_idx: int, handle_type: str) -> HANDLE:
    return HANDLE(
        CR2WFILE=cr2w, ChunkHandle=True, ClassName=None,
        DepotPath=None, Flags=None, Index=None,
        Reference=ref_idx, theType=handle_type, val=ref_idx,
    )


def _make_null_handle(cr2w: CR2W, handle_type: str) -> HANDLE:
    return HANDLE(
        CR2WFILE=cr2w, ChunkHandle=True, ClassName=None,
        DepotPath=None, Flags=None, Index=None,
        Reference=None, theType=handle_type, val=0,
    )


# ---------------------------------------------------------------------------
# Embedded animation data builder
# ---------------------------------------------------------------------------

def build_embedded_anim_data(
    bones: List[Dict[str, List[Tuple[float, ...]]]],
    num_frames: int,
    dt: float = DEFAULT_DT,
    version: int = DEFAULT_VERSION,
    unk: int = DEFAULT_UNK,
    flags: int = DEFAULT_FLAGS,
    duration_factor: float = DEFAULT_DURATION_FACTOR,
) -> bytes:
    header = io.BytesIO()
    header.write(struct.pack("<I", int(version)))
    header.write(struct.pack("<f", float(dt)))
    header.write(struct.pack("<I", int(unk)))
    header.write(struct.pack("<I", 0))
    header.write(struct.pack("<f", float(duration_factor)))
    header.write(struct.pack("<H", int(flags)))
    header.write(b"\x00\x00\x00")
    header.write(struct.pack("<I", int(num_frames)))

    body = io.BytesIO()
    body.write(header.getvalue())

    for bone in bones:
        pos_frames = _pad_frames(bone["pos_frames"], num_frames)
        rot_frames = _pad_frames(bone["rot_frames"], num_frames)
        scale_frames = _pad_frames(bone["scale_frames"], num_frames)

        for frames, comp_count in (
            (pos_frames, 3),
            (rot_frames, 4),
            (scale_frames, 3),
        ):
            for comp_idx in range(comp_count):
                values = [frame[comp_idx] for frame in frames]
                if _component_is_constant(values):
                    body.write(b"\x01")
                    body.write(struct.pack("<f", float(values[0])))
                else:
                    body.write(b"\x00")
                    for v in values:
                        body.write(struct.pack("<f", float(v)))

    payload = body.getvalue()
    prefix = struct.pack("<H", 0) + struct.pack("<I", len(payload))
    return prefix + payload


# ---------------------------------------------------------------------------
# Shared animation chunk builder
# ---------------------------------------------------------------------------

def _make_compression_settings(cr2w: CR2W, prop_name: str = "compressionSettings") -> PROPERTY:
    """Build SAnimationBufferBitwiseCompressionSettings struct for ABBCP_Raw."""
    return PROPERTY(
        theName=prop_name,
        theType="SAnimationBufferBitwiseCompressionSettings",
        More=[
            PROPERTY(Value=0.0, theName="translationTolerance", theType="Float"),
            PROPERTY(Value=0.0, theName="translationSkipFrameTolerance", theType="Float"),
            PROPERTY(Value=0.0, theName="orientationTolerance", theType="Float"),
            _make_enum_prop(cr2w, "orientationCompressionMethod",
                           "SAnimationBufferOrientationCompressionMethod",
                           "ABOCM_AsFloat_XYZSignedWInLastBit"),
            PROPERTY(Value=0.0, theName="orientationSkipFrameTolerance", theType="Float"),
            PROPERTY(Value=0.0, theName="scaleTolerance", theType="Float"),
            PROPERTY(Value=0.0, theName="scaleSkipFrameTolerance", theType="Float"),
            PROPERTY(Value=0.0, theName="trackTolerance", theType="Float"),
            PROPERTY(Value=0.0, theName="trackSkipFrameTolerance", theType="Float"),
        ],
    )


def _build_animation_chunks(
    cr2w: CR2W,
    action_name: str,
    bones: List[Dict[str, List[Tuple[float, ...]]]],
    num_frames: int,
    dt: float,
    fps: float,
    buffer_index: int,
    skeletal_type: str = "SAT_Normal",
    additive_type: Optional[str] = None,
    motion_extraction: Optional[dict] = None,
) -> dict:
    """Build Entry → [MotionExtraction] → Animation → Buffer chunks for one animation.

    Returns dict with:
        entry_idx: chunk index of CSkeletalAnimationSetEntry
        num_chunks: total chunks created
        buffer_payload: raw buffer data (bytes)
    """
    use_raw = True  # ABBCP_Raw format

    anim_buffer_payload, fallback_data, bone_info = build_cooked_anim_buffer(
        bones, num_frames, use_raw=use_raw)

    # Also build uncooked embedded data (vanilla files have BOTH)
    # build_embedded_anim_data returns: 2-byte zero prefix + 4-byte length + anim data
    # Vanilla files store: 4-byte length prefix + anim data (strip only 2-byte zero prefix)
    embedded_raw = build_embedded_anim_data(bones, num_frames, dt=dt)
    embedded_payload = embedded_raw[2:]  # keep 4-byte length prefix, strip 2-byte zero

    # Full uncooked size (the raw anim data without any prefix)
    uncooked_size = len(embedded_raw) - 6

    has_me = motion_extraction is not None
    start_idx = cr2w.HEADER.numChunks

    # Vanilla chunk ordering: Entry → Anim → LineME → Buffer → UncompME
    idx_entry = start_idx
    idx_anim = start_idx + 1
    next_idx = start_idx + 2
    if has_me:
        idx_line_me = next_idx; next_idx += 1
    else:
        idx_line_me = None
    idx_buffer = next_idx; next_idx += 1
    if has_me:
        idx_uncomp_me = next_idx; next_idx += 1
    else:
        idx_uncomp_me = None

    # 1) CSkeletalAnimationSetEntry
    anim_handle = _make_handle(cr2w, idx_anim, "ptr:CSkeletalAnimation")
    animation_prop = PROPERTY(
        CR2WFILE=cr2w,
        Handles=[anim_handle], elements=[anim_handle],
        theName="animation", theType="ptr:CSkeletalAnimation",
    )
    _, entry_chunk = _add_chunk(cr2w, "CSkeletalAnimationSetEntry", [animation_prop])
    # WolvenKit reads an events array after properties; write count=0
    entry_chunk.postPropsData = struct.pack("<I", 0)

    # 2) CSkeletalAnimation (comes BEFORE motion extraction chunks in vanilla)
    anim_buffer_handle = _make_handle(cr2w, idx_buffer, "ptr:IAnimationBuffer")
    if has_me:
        motion_handle = _make_handle(cr2w, idx_line_me, "ptr:IMotionExtraction")
        uncompressed_motion_handle = _make_handle(cr2w, idx_uncomp_me, "ptr:CUncompressedMotionExtraction")
    else:
        motion_handle = _make_null_handle(cr2w, "ptr:IMotionExtraction")
        uncompressed_motion_handle = _make_null_handle(cr2w, "ptr:CUncompressedMotionExtraction")

    anim_props = [
        PROPERTY(theName="name", theType="CName", String=CSTRING(isUTF=False, String=action_name)),
        PROPERTY(Value=True, theName="useOwnBitwiseCompressionParams", theType="Bool"),
        _make_enum_prop(cr2w, "bitwiseCompressionPreset", "SAnimationBufferBitwiseCompressionPreset", "ABBCP_Raw"),
        _make_compression_settings(cr2w, "bitwiseCompressionSettings"),
        PROPERTY(CR2WFILE=cr2w, Handles=[motion_handle], elements=[motion_handle],
                 theName="motionExtraction", theType="ptr:IMotionExtraction"),
        PROPERTY(CR2WFILE=cr2w, Handles=[anim_buffer_handle], elements=[anim_buffer_handle],
                 theName="animBuffer", theType="ptr:IAnimationBuffer"),
        PROPERTY(Value=float(fps), theName="framesPerSecond", theType="Float"),
        PROPERTY(Value=float(dt * max(num_frames - 1, 0)), theName="duration", theType="Float"),
        PROPERTY(CR2WFILE=cr2w, Handles=[uncompressed_motion_handle], elements=[uncompressed_motion_handle],
                 theName="uncompressedMotionExtraction", theType="ptr:CUncompressedMotionExtraction"),
    ]
    if additive_type:
        anim_props.append(_make_enum_prop(cr2w, "Additive type for reimport", "EAdditiveType", additive_type))
    _, anim_chunk = _add_chunk(cr2w, "CSkeletalAnimation", anim_props)
    anim_chunk.embeddedAnimData = embedded_payload

    # 3) Optional CLineMotionExtraction2 (after CSkeletalAnimation in vanilla)
    if has_me:
        me = motion_extraction
        required_keys = ("duration", "frames", "delta_times", "flags")
        missing = [k for k in required_keys if k not in me]
        if missing:
            raise KeyError(f"Motion extraction data missing required keys: {missing}")

        duration = float(me["duration"])

        frame_elements = [PROPERTY(Value=float(v), theType="Float") for v in me["frames"]]
        delta_elements = [PROPERTY(Value=int(v), theType="Uint8") for v in me["delta_times"]]
        line_me_props = [
            PROPERTY(Value=duration, theName="duration", theType="Float"),
            PROPERTY(theName="frames", theType="array:2,0,Float", elements=frame_elements),
            PROPERTY(theName="deltaTimes", theType="array:2,0,Uint8", elements=delta_elements),
            PROPERTY(Value=int(me["flags"]), theName="flags", theType="Uint8"),
        ]
        _add_chunk(cr2w, "CLineMotionExtraction2", line_me_props)

    # 4) CAnimationBufferBitwiseCompressed
    bone_elements = []
    for bone_idx, bone in enumerate(bones):
        info = bone_info[bone_idx]

        # Position: compression=2 (Float16) for ABBCP_Raw
        # Only set dataAddr/dataAddrFallback when non-zero
        pos_props = [
            PROPERTY(Value=float(dt), theName="dt", theType="Float"),
            PROPERTY(Value=2, theName="compression", theType="Int8"),
            PROPERTY(Value=int(num_frames if info['pos_multi'] else 1), theName="numFrames", theType="Uint16"),
        ]
        if info['pos_addr'] > 0:
            pos_props.append(PROPERTY(Value=info['pos_addr'], theName="dataAddr", theType="Uint32"))
        if info['pos_addr_fb'] > 0:
            pos_props.append(PROPERTY(Value=info['pos_addr_fb'], theName="dataAddrFallback", theType="Uint32"))

        # Rotation: no compression field (handled by orientationCompressionMethod)
        rot_props = [
            PROPERTY(Value=float(dt), theName="dt", theType="Float"),
            PROPERTY(Value=int(num_frames if info['rot_multi'] else 1), theName="numFrames", theType="Uint16"),
            PROPERTY(Value=info['rot_addr'], theName="dataAddr", theType="Uint32"),
            PROPERTY(Value=info['rot_addr_fb'], theName="dataAddrFallback", theType="Uint32"),
        ]

        # Scale: compression=2 (Float16, same in RAW format)
        scale_props = [
            PROPERTY(Value=float(dt), theName="dt", theType="Float"),
            PROPERTY(Value=2, theName="compression", theType="Int8"),
            PROPERTY(Value=int(num_frames if info['scale_multi'] else 1), theName="numFrames", theType="Uint16"),
            PROPERTY(Value=info['scale_addr'], theName="dataAddr", theType="Uint32"),
            PROPERTY(Value=info['scale_addr_fb'], theName="dataAddrFallback", theType="Uint32"),
        ]

        bone_track = PROPERTY(
            theName="SAnimationBufferBitwiseCompressedBoneTrack",
            theType="SAnimationBufferBitwiseCompressedBoneTrack",
            More=[
                PROPERTY(theName="position", theType="SAnimationBufferBitwiseCompressedData", More=pos_props),
                PROPERTY(theName="orientation", theType="SAnimationBufferBitwiseCompressedData", More=rot_props),
                PROPERTY(theName="scale", theType="SAnimationBufferBitwiseCompressedData", More=scale_props),
            ],
        )
        bone_elements.append(bone_track)

    bones_prop = PROPERTY(
        theName="bones",
        theType="array:134,0,SAnimationBufferBitwiseCompressedBoneTrack",
        elements=bone_elements,
    )

    # FallbackData: compact first-frame buffer as Int8 array
    # Convert unsigned bytes (0-255) to signed Int8 (-128 to 127)
    fallback_elements = [PROPERTY(Value=(b if b < 128 else b - 256), theType="Int8") for b in fallback_data]
    fallback_prop = PROPERTY(
        theName="fallbackData",
        theType="array:134,0,Int8",
        elements=fallback_elements,
    )

    buffer_props = [
        _make_enum_prop(cr2w, "compressionPreset", "SAnimationBufferBitwiseCompressionPreset", "ABBCP_Raw"),
        _make_compression_settings(cr2w),
        PROPERTY(Value=uncooked_size, theName="sourceDataSize", theType="Uint32"),
        PROPERTY(Value=2, theName="version", theType="Uint32"),
        bones_prop,
        fallback_prop,
        PROPERTY(ValueA=buffer_index, theName="deferredData", theType="DeferredDataBuffer"),
        _make_enum_prop(cr2w, "orientationCompressionMethod",
                       "SAnimationBufferOrientationCompressionMethod",
                       "ABOCM_AsFloat_XYZSignedWInLastBit"),
        PROPERTY(Value=float(dt * max(num_frames - 1, 0)), theName="duration", theType="Float"),
        PROPERTY(Value=int(num_frames), theName="numFrames", theType="Uint32"),
        PROPERTY(Value=float(dt), theName="dt", theType="Float"),
        _make_enum_prop(cr2w, "streamingOption", "SAnimationBufferStreamingOption", "ABSO_FullyStreamable"),
        PROPERTY(Value=True, theName="hasRefIKBones", theType="Bool"),
    ]
    _add_chunk(cr2w, "CAnimationBufferBitwiseCompressed", buffer_props)

    # 5) Optional CUncompressedMotionExtraction (last chunk in vanilla ordering)
    if has_me:
        uncomp_frames = me.get("uncompressed_frames", [])
        vector_elements = [
            _make_vector_prop("Vector", f[0], f[1], f[2], f[3] if len(f) > 3 else 0.0)
            for f in uncomp_frames
        ]
        uncomp_me_props = [
            PROPERTY(theName="frames", theType="array:2,0,Vector", elements=vector_elements),
            PROPERTY(Value=duration, theName="duration", theType="Float"),
        ]
        _add_chunk(cr2w, "CUncompressedMotionExtraction", uncomp_me_props)

    return {
        "entry_idx": idx_entry,
        "num_chunks": next_idx - start_idx,
        "buffer_payload": anim_buffer_payload,
    }


# ---------------------------------------------------------------------------
# Public API: build_w2anims
# ---------------------------------------------------------------------------

def build_w2anims(
    action_name: str,
    bones: List[Dict[str, List[Tuple[float, ...]]]],
    num_frames: int,
    dt: float = DEFAULT_DT,
    fps: float = 30.0,
    skeletal_type: str = "SAT_Normal",
    additive_type: Optional[str] = None,
    motion_extraction: Optional[dict] = None,
    header_version: int = DEFAULT_HEADER_VERSION,
    build_version: int = DEFAULT_BUILD_VERSION,
) -> CR2W:
    cr2w = _init_cr2w(header_version, build_version)

    # Chunk 0: CSkeletalAnimationSet (root)
    # Entry handle index = 1 (always the next chunk after root)
    entry_handle = _make_handle(cr2w, 1, "ptr:CSkeletalAnimationSetEntry")
    animations_prop = PROPERTY(
        CR2WFILE=cr2w,
        Handles=[entry_handle], elements=[entry_handle],
        theName="animations", theType="array:2,0,ptr:CSkeletalAnimationSetEntry",
    )
    ext_events_prop = PROPERTY(
        CR2WFILE=cr2w, Handles=[], elements=[],
        theName="extAnimEvents", theType="array:2,0,handle:CExtAnimEventsFile",
    )
    streaming_prop = _make_enum_prop(cr2w, "Streaming option", "SAnimationBufferStreamingOption", "ABSO_FullyStreamable")
    _, set_chunk = _add_chunk(cr2w, "CSkeletalAnimationSet", [animations_prop, ext_events_prop, streaming_prop])
    # Vanilla files have 4 trailing bytes after properties
    set_chunk.postPropsData = struct.pack("<I", 0)

    # Animation chunks (Entry → [ME] → Animation → Buffer) starting at index 1
    result = _build_animation_chunks(
        cr2w,
        action_name=action_name,
        bones=bones,
        num_frames=num_frames,
        dt=dt,
        fps=fps,
        buffer_index=1,
        skeletal_type=skeletal_type,
        additive_type=additive_type,
        motion_extraction=motion_extraction,
    )

    # Populate buffer table
    payload = result["buffer_payload"]
    cr2w.CR2WBuffer = [CR2WBuffer(index=1, diskSize=len(payload), memSize=len(payload))]
    cr2w.BufferData = [payload]

    return cr2w


# ---------------------------------------------------------------------------
# Public API: build_w2cutscene
# ---------------------------------------------------------------------------

def build_w2cutscene(
    actors: List[Dict],
    animations: List[Dict],
    header_version: int = DEFAULT_HEADER_VERSION,
    build_version: int = DEFAULT_BUILD_VERSION,
) -> CR2W:
    """Build a .w2cutscene CR2W file.

    actors: list of dicts with keys:
        name (str), template (str, depot path), type (str, e.g. "CAT_Actor")
    animations: list of dicts with keys:
        actor (str), component (str, e.g. "Root"), action_name (str),
        bones (list), num_frames (int), dt (float), fps (float),
        skeletal_type (str), additive_type (str or None),
        motion_extraction (dict or None)
    """
    cr2w = _init_cr2w(header_version, build_version)

    # We build the CCutsceneTemplate root chunk first, but we need to know
    # the entry chunk indices for each animation to build the animations array.
    # Strategy: reserve chunk 0 for the root, build all animation chunks,
    # then go back and fill in the root chunk's animations array.

    # Reserve root chunk slot (we'll fill props after building animation chunks)
    root_idx = cr2w.HEADER.numChunks
    cr2w.HEADER.numChunks += 1
    cr2w.CR2WExport.append(
        CR2WExport(
            crc32=0, dataOffset=0, dataSize=0, name="CCutsceneTemplate",
            objectFlags=0, parentID=0, template=0,
        )
    )
    # Placeholder — will be replaced after we know all entry indices
    root_chunk = W_CLASS(CR2WFILE=cr2w, idx=root_idx, PROPS=[], Type="CCutsceneTemplate", name="CCutsceneTemplate")
    cr2w.CHUNKS.CHUNKS.append(root_chunk)

    # Build animation chunks for each actor animation
    entry_indices = []
    buffer_payloads = []
    buffer_index = 1  # buffer indices are 1-based

    for anim in animations:
        actor_name = anim["actor"]
        component = anim.get("component", "Root")
        action_name = anim["action_name"]
        full_name = f"{actor_name}:{component}:{action_name}"

        result = _build_animation_chunks(
            cr2w,
            action_name=full_name,
            bones=anim["bones"],
            num_frames=anim["num_frames"],
            dt=anim.get("dt", DEFAULT_DT),
            fps=anim.get("fps", 30.0),
            buffer_index=buffer_index,
            skeletal_type=anim.get("skeletal_type", "SAT_Normal"),
            additive_type=anim.get("additive_type", None),
            motion_extraction=anim.get("motion_extraction", None),
        )
        entry_indices.append(result["entry_idx"])
        buffer_payloads.append(result["buffer_payload"])
        buffer_index += 1

    # Build the animations ptr array for CCutsceneTemplate
    entry_handles = []
    for eidx in entry_indices:
        h = _make_handle(cr2w, eidx, "ptr:CSkeletalAnimationSetEntry")
        entry_handles.append(h)

    animations_prop = PROPERTY(
        CR2WFILE=cr2w,
        Handles=entry_handles, elements=entry_handles,
        theName="animations", theType="array:2,0,ptr:CSkeletalAnimationSetEntry",
    )

    # Build actorsDef array
    actor_def_elements = []
    for actor in actors:
        actor_props = [
            PROPERTY(theName="name", theType="String", String=CSTRING(isUTF=False, String=actor["name"])),
            _make_enum_prop(cr2w, "type", "ECutsceneActorType", actor.get("type", "CAT_Actor")),
        ]
        actor_def = PROPERTY(
            theName="SCutsceneActorDef", theType="SCutsceneActorDef",
            More=actor_props,
        )
        actor_def_elements.append(actor_def)

    actors_def_prop = PROPERTY(
        theName="actorsDef",
        theType="array:124,0,SCutsceneActorDef",
        elements=actor_def_elements,
    )

    # Set root chunk properties
    root_chunk.PROPS = [animations_prop, actors_def_prop]

    # Populate buffer table
    cr2w.CR2WBuffer = []
    cr2w.BufferData = []
    for i, payload in enumerate(buffer_payloads):
        cr2w.CR2WBuffer.append(CR2WBuffer(index=i + 1, diskSize=len(payload), memSize=len(payload)))
        cr2w.BufferData.append(payload)

    return cr2w
