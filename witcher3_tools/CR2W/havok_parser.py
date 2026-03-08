"""
Havok 6.5.0 Packfile Parser
============================

Parses embedded Havok packfiles found in Witcher 2 uncooked resource files
(.w2rig, .w2anims, etc.).

Havok packfiles (magic 0x57E0E057) contain serialized Havok objects such as
hkaSkeleton, hkaAnimation, etc. This module provides utilities to:

  - Locate and parse the packfile header
  - Parse section headers (__classnames__, __types__, __data__)
  - Resolve local, global, and virtual fixups
  - Extract hkaSkeleton data (bone names, parent indices, reference pose)

The packfile layout for Havok 6.5.0 with 32-bit pointers is:

  Header (64 bytes):
    [0x00] magic           4B  0x57E0E057
    [0x04] user_tag        4B  0x10C0C010
    [0x08] file_version    4B  (usually 0 for 6.5)
    [0x0C] havok_version   4B  (6 = Havok 6.x)
    [0x10] pointer_size    1B  (4)
    [0x11] endian          1B  (1 = little-endian)
    [0x12] padding         1B
    [0x13] base_class      1B
    [0x14] section_count   4B  (3: classnames, types, data)
    [0x18] content_sect_ix 4B  (2 = data section)
    [0x1C] content_offset  4B  (0)
    [0x20] classname_ix    4B  (0)
    [0x24] classname_off   4B
    [0x28] version_string  ~16B  "Havok-6.5.0-r1" + 0xFF padding

  Section Headers (48 bytes each x section_count):
    [0x00] tag             16B  null-padded ASCII ("__classnames__", etc.)
    [0x10] separator       4B   (0x000000FF)
    [0x14] abs_data_start  4B   absolute offset from havok blob start to section data
    [0x18] local_fixups    4B   relative to abs_data_start
    [0x1C] global_fixups   4B   relative to abs_data_start
    [0x20] virtual_fixups  4B   relative to abs_data_start
    [0x24] exports         4B   relative to abs_data_start
    [0x28] imports         4B   relative to abs_data_start
    [0x2C] end             4B   relative to abs_data_start

  Section Data:
    Stored sequentially, separated by 0xFF padding to 16-byte alignment.
    The __data__ section contains the serialized hkaSkeleton object.

  Fixup Tables (within each section, appended after content data):
    - Local fixups:   8-byte pairs (src_off, dst_off), both relative to
                      the section's abs_data_start.
    - Global fixups:  12-byte triples (src_off, dst_section_idx, dst_off),
                      src relative to section, dst relative to target section.
    - Virtual fixups: 12-byte triples (data_off, classnames_section_idx,
                      classname_off), assigns vtable class to object at data_off.

Usage:
    from witcher3_tools.CR2W.havok_parser import HavokPackfile

    with open("file.w2rig", "rb") as f:
        data = f.read()

    # Find and parse the embedded Havok packfile
    packfile = HavokPackfile.from_data(data)
    if packfile:
        skeleton = packfile.read_skeleton()
        if skeleton:
            print(skeleton.names)
            print(skeleton.parent_indices)
"""

import struct
import logging

try:
    from .havok_spline_decompressor import decompress_spline_animation
except ImportError:
    # Supports direct module execution from CR2W folder in local debug scripts.
    from havok_spline_decompressor import decompress_spline_animation

log = logging.getLogger(__name__)

HAVOK_MAGIC = 0x57E0E057


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------

class HavokSkeleton:
    """Skeleton data extracted from an hkaSkeleton Havok object."""

    __slots__ = ('names', 'parent_indices', 'positions', 'rotations', 'scales',
                 'num_bones', 'skeleton_name')

    def __init__(self):
        self.skeleton_name = ""
        self.num_bones = 0
        self.names = []           # list[str]
        self.parent_indices = []  # list[int]  (-1 for root)
        self.positions = []       # list[tuple(x, y, z, w)]
        self.rotations = []       # list[tuple(x, y, z, w)]
        self.scales = []          # list[tuple(x, y, z, w)]

    def __repr__(self):
        return (f"HavokSkeleton(name={self.skeleton_name!r}, "
                f"bones={self.num_bones})")


class HavokSection:
    """A parsed section header from the Havok packfile."""

    __slots__ = ('tag', 'abs_data_start', 'local_fixups', 'global_fixups',
                 'virtual_fixups', 'exports', 'imports', 'end')

    def __init__(self, tag, fields):
        self.tag = tag
        self.abs_data_start = fields[0]
        self.local_fixups = fields[1]
        self.global_fixups = fields[2]
        self.virtual_fixups = fields[3]
        self.exports = fields[4]
        self.imports = fields[5]
        self.end = fields[6]

    def __repr__(self):
        return (f"HavokSection(tag={self.tag!r}, "
                f"data_start=0x{self.abs_data_start:X}, "
                f"end=0x{self.end:X})")


# ---------------------------------------------------------------------------
# Main packfile parser
# ---------------------------------------------------------------------------

class HavokPackfile:
    """Parser for a Havok 6.5.0 packfile embedded within a byte buffer.

    Attributes:
        data:           The full byte buffer containing the packfile.
        offset:         Byte offset where the packfile starts in `data`.
        pointer_size:   Pointer width (4 or 8 bytes).
        little_endian:  Whether data is little-endian.
        section_count:  Number of sections.
        sections:       List of HavokSection objects.
        version_string: E.g. "Havok-6.5.0-r1".
    """

    HEADER_SIZE = 64
    SECTION_HEADER_SIZE = 48

    def __init__(self, data, offset=0):
        self.data = data
        self.offset = offset
        self.pointer_size = 4
        self.little_endian = True
        self.section_count = 0
        self.sections = []
        self.version_string = ""
        self._fixup_cache = {}

    # -- Construction helpers ------------------------------------------------

    @classmethod
    def find_magic(cls, data, start=0):
        """Return the byte offset of the first Havok magic in `data`, or -1."""
        magic_bytes = struct.pack('<I', HAVOK_MAGIC)
        return data.find(magic_bytes, start)

    @classmethod
    def from_data(cls, data, start=0):
        """Find and parse the first Havok packfile in `data`.

        Returns a HavokPackfile instance, or None if no packfile found.
        """
        offset = cls.find_magic(data, start)
        if offset < 0:
            return None
        packfile = cls(data, offset)
        if not packfile._parse_header():
            return None
        packfile._parse_section_headers()
        return packfile

    # -- Header parsing ------------------------------------------------------

    def _u32(self, off):
        """Read a uint32 at absolute offset."""
        fmt = '<I' if self.little_endian else '>I'
        return struct.unpack_from(fmt, self.data, off)[0]

    def _i16(self, off):
        """Read an int16 at absolute offset."""
        fmt = '<h' if self.little_endian else '>h'
        return struct.unpack_from(fmt, self.data, off)[0]

    def _cstring(self, off, max_len=256):
        """Read a null-terminated ASCII string at absolute offset."""
        end = off
        limit = min(off + max_len, len(self.data))
        while end < limit and self.data[end] != 0:
            end += 1
        return self.data[off:end].decode('ascii', errors='replace')

    def _parse_header(self):
        """Parse the 64-byte packfile header. Returns True on success."""
        base = self.offset

        magic = struct.unpack_from('<I', self.data, base)[0]
        if magic != HAVOK_MAGIC:
            log.error("Invalid Havok magic: 0x%08X", magic)
            return False

        self.little_endian = (self.data[base + 0x11] == 1)
        self.pointer_size = self.data[base + 0x10]

        self.section_count = self._u32(base + 0x14)
        if self.section_count < 1 or self.section_count > 10:
            log.error("Unusual section count: %d", self.section_count)
            return False

        self.version_string = self._cstring(base + 0x28, 24)

        log.debug("Havok packfile: %s, %d sections, ptr_size=%d, LE=%s",
                  self.version_string, self.section_count,
                  self.pointer_size, self.little_endian)
        return True

    def _parse_section_headers(self):
        """Parse section headers that follow immediately after the 64-byte header."""
        self.sections = []
        base = self.offset + self.HEADER_SIZE

        for i in range(self.section_count):
            sh_off = base + i * self.SECTION_HEADER_SIZE

            tag_raw = self.data[sh_off:sh_off + 16]
            tag = tag_raw.split(b'\x00')[0].decode('ascii', errors='replace')

            fields_off = sh_off + 20  # 16 (tag) + 4 (separator)
            endian_prefix = '<' if self.little_endian else '>'
            fields = struct.unpack_from(f'{endian_prefix}7I', self.data, fields_off)

            section = HavokSection(tag, fields)
            self.sections.append(section)
            log.debug("  Section[%d]: %s", i, section)

    # -- Section accessors ---------------------------------------------------

    def get_section(self, tag):
        """Return the HavokSection with the given tag, or None."""
        for s in self.sections:
            if s.tag == tag:
                return s
        return None

    @property
    def data_section(self):
        return self.get_section('__data__')

    @property
    def classnames_section(self):
        return self.get_section('__classnames__')

    # -- Fixup resolution ----------------------------------------------------

    def _build_fixup_map(self, section):
        """Build a dict mapping src_offset -> dst_offset for local fixups.

        Both offsets are relative to the section's abs_data_start.

        Section header offset fields (local_fixups, global_fixups, etc.)
        are stored relative to abs_data_start, so the absolute position
        of the local fixup table in the buffer is:
            self.offset + section.abs_data_start + section.local_fixups
        """
        cache_key = ('local', section.tag)
        if cache_key in self._fixup_cache:
            return self._fixup_cache[cache_key]

        fixup_map = {}
        section_base = self.offset + section.abs_data_start
        local_fix_abs = section_base + section.local_fixups
        global_fix_abs = section_base + section.global_fixups

        num_fixups = (global_fix_abs - local_fix_abs) // 8
        endian_fmt = '<II' if self.little_endian else '>II'
        for i in range(num_fixups):
            off = local_fix_abs + i * 8
            src, dst = struct.unpack_from(endian_fmt, self.data, off)
            if src != 0xFFFFFFFF:  # skip sentinel entries
                fixup_map[src] = dst

        self._fixup_cache[cache_key] = fixup_map
        log.debug("  Built local fixup map for %s: %d entries", section.tag, len(fixup_map))
        return fixup_map

    def _build_global_fixup_map(self, section):
        """Build global fixup map: src_offset -> (dst_section_idx, dst_offset).

        Global fixups are 12-byte triples stored between global_fixups and
        virtual_fixups boundaries. src is relative to this section's data,
        dst_offset is relative to the target section's data.
        """
        cache_key = ('global', section.tag)
        if cache_key in self._fixup_cache:
            return self._fixup_cache[cache_key]

        gfmap = {}
        section_base = self.offset + section.abs_data_start
        global_fix_abs = section_base + section.global_fixups
        virtual_fix_abs = section_base + section.virtual_fixups

        num = (virtual_fix_abs - global_fix_abs) // 12
        endian = '<' if self.little_endian else '>'
        for i in range(num):
            off = global_fix_abs + i * 12
            src, dst_sect, dst_off = struct.unpack_from(f'{endian}III', self.data, off)
            if src != 0xFFFFFFFF:
                gfmap[src] = (dst_sect, dst_off)

        self._fixup_cache[cache_key] = gfmap
        log.debug("  Built global fixup map for %s: %d entries", section.tag, len(gfmap))
        return gfmap

    def _build_virtual_fixup_map(self, section):
        """Build virtual fixup map: data_offset -> classname string.

        Virtual fixups are 12-byte triples (data_offset, classnames_sect_idx,
        classname_offset) stored between virtual_fixups and end offsets.
        They identify the Havok class of each top-level object.
        """
        cache_key = ('virtual', section.tag)
        if cache_key in self._fixup_cache:
            return self._fixup_cache[cache_key]

        vfmap = {}
        section_base = self.offset + section.abs_data_start
        virtual_fix_abs = section_base + section.virtual_fixups
        end_abs = section_base + section.end

        cn_sect = self.classnames_section
        cn_base = self.offset + cn_sect.abs_data_start if cn_sect else 0

        num = (end_abs - virtual_fix_abs) // 12
        endian = '<' if self.little_endian else '>'
        for i in range(num):
            off = virtual_fix_abs + i * 12
            data_off, sect_idx, cn_off = struct.unpack_from(f'{endian}III', self.data, off)
            if data_off != 0xFFFFFFFF:
                cn_str_abs = cn_base + cn_off
                classname = self._cstring(cn_str_abs, 64)
                vfmap[data_off] = classname

        self._fixup_cache[cache_key] = vfmap
        log.debug("  Built virtual fixup map for %s: %d entries", section.tag, len(vfmap))
        return vfmap

    # -- Skeleton extraction -------------------------------------------------

    def read_skeleton(self):
        """Extract the hkaSkeleton from the __data__ section.

        Returns a HavokSkeleton, or None on failure.

        hkaSkeleton layout (Havok 6.5.0, serialized form):

        In Havok 6.5 serialized packfiles, objects do NOT have an inline
        vtable or memSizeAndFlags prefix — the vtable class is assigned via
        virtual fixups. hkArray fields are stored as 8 bytes (ptr + size)
        with no capacityAndFlags field.

          Offset | Size | Field
          -------|------|------
           0x00  |  4   | name pointer          -> null-terminated string
           0x04  |  4   | parentIndices.ptr      -> int16 array
           0x08  |  4   | parentIndices.size
           0x0C  |  4   | bones.ptr              -> pointer table to hkaBone objects
           0x10  |  4   | bones.size
           0x14  |  4   | referencePose.ptr      -> QsTransform array
           0x18  |  4   | referencePose.size
           0x1C  |  4   | referenceFloats.ptr
           0x20  |  4   | referenceFloats.size
           0x24  |  4   | floatSlots.ptr
           0x28  |  4   | floatSlots.size

        hkaBone (32 bytes, 0x7F-padded):
           0x00  |  4   | name pointer  -> null-terminated string
           0x04  |  4   | lockTranslation (hkBool)
           0x08  | 24   | 0x7F padding

        QsTransform (48 bytes):
           0x00  | 16   | position  (vec4: x, y, z, w)
           0x10  | 16   | rotation  (quat: x, y, z, w)
           0x20  | 16   | scale     (vec4: x, y, z, w)
        """
        ds = self.data_section
        if ds is None:
            log.error("No __data__ section found")
            return None

        fixup_map = self._build_fixup_map(ds)
        data_abs = self.offset + ds.abs_data_start

        # Find the hkaSkeleton object
        skeleton_offset = self._find_skeleton_object(fixup_map, ds)
        if skeleton_offset is None:
            log.error("Could not locate hkaSkeleton in data section")
            return None

        skel_abs = data_abs + skeleton_offset
        skel = HavokSkeleton()

        # Havok 6.5 serialized hkArray is 8 bytes: ptr(4) + size(4)
        parent_count = self._u32(skel_abs + 0x08)   # parentIndices.size
        bones_count = self._u32(skel_abs + 0x10)     # bones.size
        ref_pose_count = self._u32(skel_abs + 0x18)  # referencePose.size

        log.debug("  hkaSkeleton at data offset 0x%X: parent=%d, bones=%d, pose=%d",
                  skeleton_offset, parent_count, bones_count, ref_pose_count)

        if bones_count == 0 or bones_count > 500:
            log.error("Invalid bone count: %d", bones_count)
            return None

        skel.num_bones = bones_count

        # Read skeleton name (at +0x00)
        name_ptr_fixup = fixup_map.get(skeleton_offset + 0x00)
        if name_ptr_fixup is not None:
            skel.skeleton_name = self._cstring(data_abs + name_ptr_fixup)

        # ------ Parent indices (int16 array at +0x04) ------
        parent_ptr_fixup = fixup_map.get(skeleton_offset + 0x04)
        if parent_ptr_fixup is not None:
            arr_abs = data_abs + parent_ptr_fixup
            endian = '<' if self.little_endian else '>'
            skel.parent_indices = list(
                struct.unpack_from(f'{endian}{parent_count}h', self.data, arr_abs)
            )
        else:
            log.warning("No fixup for parentIndices pointer")
            skel.parent_indices = [-1] + [0] * (bones_count - 1)

        # ------ Bone names ------
        # bones.ptr at +0x0C points to a table of pointers, one per bone.
        # Each pointer is resolved by a global fixup to an hkaBone object.
        # Each hkaBone object has a name pointer resolved by a local fixup.
        bones_ptr_fixup = fixup_map.get(skeleton_offset + 0x0C)
        if bones_ptr_fixup is not None:
            gfmap = self._build_global_fixup_map(ds)
            for i in range(bones_count):
                ptr_table_off = bones_ptr_fixup + i * 4
                # Global fixup: pointer table entry -> hkaBone object
                gf = gfmap.get(ptr_table_off)
                if gf is not None:
                    _, bone_obj_off = gf
                    # Local fixup: hkaBone.name ptr -> string
                    name_fixup = fixup_map.get(bone_obj_off)
                    if name_fixup is not None:
                        name = self._cstring(data_abs + name_fixup)
                        skel.names.append(name)
                    else:
                        skel.names.append(f"bone_{i}")
                else:
                    # Fallback: try direct local fixup (inline hkaBone array)
                    name_fixup = fixup_map.get(ptr_table_off)
                    if name_fixup is not None:
                        name = self._cstring(data_abs + name_fixup)
                        skel.names.append(name)
                    else:
                        skel.names.append(f"bone_{i}")
        else:
            log.warning("No fixup for bones array pointer")
            skel.names = [f"bone_{i}" for i in range(bones_count)]

        # ------ Reference pose (QsTransform array at +0x14, 48 bytes each) ------
        ref_pose_fixup = fixup_map.get(skeleton_offset + 0x14)
        if ref_pose_fixup is not None:
            arr_abs = data_abs + ref_pose_fixup
            endian = '<' if self.little_endian else '>'
            for i in range(bones_count):
                off = arr_abs + i * 48
                px, py, pz, pw = struct.unpack_from(f'{endian}4f', self.data, off)
                rx, ry, rz, rw = struct.unpack_from(f'{endian}4f', self.data, off + 16)
                sx, sy, sz, sw = struct.unpack_from(f'{endian}4f', self.data, off + 32)
                skel.positions.append((px, py, pz, pw))
                skel.rotations.append((rx, ry, rz, rw))
                skel.scales.append((sx, sy, sz, sw))
        else:
            log.warning("No fixup for referencePose pointer")
            for _ in range(bones_count):
                skel.positions.append((0.0, 0.0, 0.0, 0.0))
                skel.rotations.append((0.0, 0.0, 0.0, 1.0))
                skel.scales.append((1.0, 1.0, 1.0, 0.0))

        return skel

    def _find_skeleton_object(self, fixup_map, data_section):
        """Find the data-section-relative offset of the hkaSkeleton object.

        Uses virtual fixups to identify object classes by name. Falls back
        to pattern scanning if virtual fixups are unavailable.
        """
        data_abs = self.offset + data_section.abs_data_start

        # Approach 1: Use virtual fixups to find the hkaSkeleton class.
        vfmap = self._build_virtual_fixup_map(data_section)
        for data_off, classname in vfmap.items():
            if classname == 'hkaSkeleton':
                log.debug("  Found hkaSkeleton via virtual fixup at data offset 0x%X",
                          data_off)
                return data_off

        # Approach 2: Pattern scan — look for an offset with fixups at
        # +0x00 (name), +0x04 (parentIndices), +0x0C (bones), +0x14 (referencePose)
        # and matching sizes at +0x08, +0x10, +0x18.
        for src_off in sorted(fixup_map.keys()):
            base = src_off
            has_name = base in fixup_map
            has_parents = (base + 0x04) in fixup_map
            has_bones = (base + 0x0C) in fixup_map
            has_pose = (base + 0x14) in fixup_map

            if has_name and has_parents and has_bones and has_pose:
                candidate_abs = data_abs + base
                if candidate_abs + 0x1C <= len(self.data):
                    parent_count = self._u32(candidate_abs + 0x08)
                    bones_count = self._u32(candidate_abs + 0x10)
                    pose_count = self._u32(candidate_abs + 0x18)
                    if (0 < bones_count <= 500 and
                            parent_count == bones_count and
                            pose_count == bones_count):
                        log.debug("  Found skeleton via pattern scan at data offset 0x%X",
                                  base)
                        return base

        log.error("Could not find hkaSkeleton object in data section")
        return None

    # -- Animation extraction ------------------------------------------------

    def read_animation_info(self):
        """Extract animation metadata from the first hkaAnimation object.

        Returns a HavokAnimationInfo, or None if no animation found.

        hkaSplineCompressedAnimation layout (Havok 6.5.0, 32-bit serialized):

          Offset | Size | Field
          -------|------|------
           0x00  |  4   | vtable placeholder (0, assigned via virtual fixup)
           0x04  |  4   | padding / memSizeAndFlags (0)
           0x08  |  4   | type (hkEnum<AnimationType, int32>): 5 = SPLINE_COMPRESSED
           0x0C  |  4   | duration (hkReal / float)
           0x10  |  4   | numberOfTransformTracks (int32)
           0x14  |  4   | numberOfFloatTracks (int32)
           0x18  |  4   | extractedMotion pointer (hkRefPtr)
           0x1C  |  4   | annotationTracks.ptr
           0x20  |  4   | annotationTracks.size
           0x24  |  4   | numFrames (int32, spline-specific)
        """
        ds = self.data_section
        if ds is None:
            return None

        data_abs = self.offset + ds.abs_data_start

        # Find animation object via virtual fixups
        vfmap = self._build_virtual_fixup_map(ds)
        anim_offset = None
        anim_class = None
        for data_off, classname in vfmap.items():
            if 'Animation' in classname and 'Binding' not in classname and 'Skeleton' not in classname and 'ReferenceFrame' not in classname:
                anim_offset = data_off
                anim_class = classname
                break

        if anim_offset is None:
            return None

        abs_off = data_abs + anim_offset
        if abs_off + 0x28 > len(self.data):
            return None

        endian = '<' if self.little_endian else '>'
        anim_type, duration, num_tracks, num_float_tracks = struct.unpack_from(
            f'{endian}ifii', self.data, abs_off + 0x08
        )

        # Try to read numFrames at +0x24 (after annotationTracks array)
        num_frames = 0
        if abs_off + 0x28 <= len(self.data):
            num_frames = struct.unpack_from(f'{endian}i', self.data, abs_off + 0x24)[0]

        # Validate: duration should be positive, tracks should be reasonable
        if duration <= 0.0 or duration > 3600.0:
            duration = 0.0
        if num_tracks < 0 or num_tracks > 1000:
            num_tracks = 0
        if num_frames < 0 or num_frames > 100000:
            num_frames = 0

        info = HavokAnimationInfo()
        info.class_name = anim_class
        info.animation_type = anim_type
        info.duration = duration
        info.num_transform_tracks = num_tracks
        info.num_float_tracks = num_float_tracks
        info.num_frames = num_frames

        log.debug("  Animation: %s type=%d duration=%.3fs tracks=%d frames=%d",
                   anim_class, anim_type, duration, num_tracks, num_frames)
        return info

    def _find_spline_animation_object(self, data_section):
        """Find the data-section-relative offset of hkaSplineCompressedAnimation."""
        vfmap = self._build_virtual_fixup_map(data_section)
        for data_off, classname in vfmap.items():
            if classname == "hkaSplineCompressedAnimation":
                return data_off
        return None

    def _read_u32_array(self, data_abs, ptr_off, count):
        """Read a uint32 array from data-section-relative pointer + count."""
        if ptr_off is None or count <= 0:
            return []
        arr_abs = data_abs + ptr_off
        arr_end = arr_abs + count * 4
        if arr_abs < 0 or arr_end > len(self.data):
            return []
        endian = '<' if self.little_endian else '>'
        return list(struct.unpack_from(f'{endian}{count}I', self.data, arr_abs))

    def read_full_animation(self, bone_names=None):
        """Decode hkaSplineCompressedAnimation into a Blender-friendly buffer.

        Returns:
            CAnimationBufferBitwiseCompressed or None
        """
        ds = self.data_section
        if ds is None:
            return None

        anim_offset = self._find_spline_animation_object(ds)
        if anim_offset is None:
            return None

        data_abs = self.offset + ds.abs_data_start
        abs_off = data_abs + anim_offset
        if abs_off + 0x78 > len(self.data):
            return None

        endian = '<' if self.little_endian else '>'

        anim_type = struct.unpack_from(f'{endian}i', self.data, abs_off + 0x08)[0]
        duration = struct.unpack_from(f'{endian}f', self.data, abs_off + 0x0C)[0]
        num_tracks = struct.unpack_from(f'{endian}i', self.data, abs_off + 0x10)[0]
        num_float_tracks = struct.unpack_from(f'{endian}i', self.data, abs_off + 0x14)[0]
        num_frames = struct.unpack_from(f'{endian}i', self.data, abs_off + 0x24)[0]
        num_blocks = struct.unpack_from(f'{endian}i', self.data, abs_off + 0x28)[0]
        max_frames_per_block = struct.unpack_from(f'{endian}i', self.data, abs_off + 0x2C)[0]
        _mask_and_quantization_size = struct.unpack_from(f'{endian}i', self.data, abs_off + 0x30)[0]
        block_duration = struct.unpack_from(f'{endian}f', self.data, abs_off + 0x34)[0]
        block_inverse_duration = struct.unpack_from(f'{endian}f', self.data, abs_off + 0x38)[0]
        frame_duration = struct.unpack_from(f'{endian}f', self.data, abs_off + 0x3C)[0]

        # Havok 6.5 (W2) uses hkArray with ptr+size+capacityAndFlags (12 bytes).
        # Offsets here match HK550-HK660 x86 layouts.
        num_block_offsets = struct.unpack_from(f'{endian}I', self.data, abs_off + 0x44)[0]
        num_float_block_offsets = struct.unpack_from(f'{endian}I', self.data, abs_off + 0x50)[0]
        num_transform_offsets = struct.unpack_from(f'{endian}I', self.data, abs_off + 0x5C)[0]
        num_float_offsets = struct.unpack_from(f'{endian}I', self.data, abs_off + 0x68)[0]
        num_data_buffer = struct.unpack_from(f'{endian}I', self.data, abs_off + 0x74)[0]

        # Sanity guards
        if anim_type != 5:
            return None
        if num_tracks <= 0 or num_tracks > 2000:
            return None
        if num_frames <= 0 or num_frames > 200000:
            return None
        if num_blocks <= 0 or num_blocks > 10000:
            return None
        if max_frames_per_block <= 0:
            max_frames_per_block = 1

        fixup_map = self._build_fixup_map(ds)
        block_offsets_ptr = fixup_map.get(anim_offset + 0x40)
        float_block_offsets_ptr = fixup_map.get(anim_offset + 0x4C)
        transform_offsets_ptr = fixup_map.get(anim_offset + 0x58)
        float_offsets_ptr = fixup_map.get(anim_offset + 0x64)
        data_ptr = fixup_map.get(anim_offset + 0x70)

        block_offsets = self._read_u32_array(data_abs, block_offsets_ptr, num_block_offsets)
        _float_block_offsets = self._read_u32_array(
            data_abs, float_block_offsets_ptr, num_float_block_offsets
        )
        _transform_offsets = self._read_u32_array(
            data_abs, transform_offsets_ptr, num_transform_offsets
        )
        _float_offsets = self._read_u32_array(data_abs, float_offsets_ptr, num_float_offsets)

        if data_ptr is None or num_data_buffer <= 0:
            log.warning("Spline animation has no data buffer pointer/size")
            return None

        data_start = data_abs + data_ptr
        data_end = data_start + num_data_buffer
        if data_start < 0 or data_end > len(self.data):
            log.warning("Spline animation data buffer outside file bounds")
            return None

        data_blob = self.data[data_start:data_end]
        if not block_offsets:
            block_offsets = [0]

        if frame_duration <= 0.0 and num_frames > 1 and duration > 0.0:
            frame_duration = duration / float(num_frames - 1)
        if frame_duration <= 0.0:
            frame_duration = 1.0 / 30.0

        try:
            bones = decompress_spline_animation(
                data_blob,
                num_tracks,
                num_float_tracks,
                num_frames,
                duration,
                frame_duration,
                block_duration,
                block_inverse_duration,
                block_offsets,
                bone_names=bone_names,
            )
        except Exception as exc:
            log.warning("Spline decompression failed: %s", exc)
            return None

        # Local import avoids hard dependency/cycle at module import time.
        try:
            from . import w3_types
            return w3_types.CAnimationBufferBitwiseCompressed(
                bones,
                [],
                duration=duration,
                numFrames=num_frames,
                dt=frame_duration,
            )
        except Exception:
            try:
                import w3_types  # type: ignore
                return w3_types.CAnimationBufferBitwiseCompressed(  # type: ignore[attr-defined]
                    bones,
                    [],
                    duration=duration,
                    numFrames=num_frames,
                    dt=frame_duration,
                )
            except Exception:
                # Standalone fallback for parser-only validation.
                class _AnimationBuffer:
                    def __init__(self, bones_, duration_, num_frames_, dt_):
                        self.bones = bones_
                        self.tracks = []
                        self.duration = duration_
                        self.numFrames = num_frames_
                        self.dt = dt_

                return _AnimationBuffer(
                    bones,
                    duration,
                    num_frames,
                    frame_duration,
                )

    @classmethod
    def _decode_packfile_animation(cls, packfile, fallback_bone_names=None):
        """Decode a single packfile animation into HavokDecodedAnimation."""
        decoded = HavokDecodedAnimation()
        if not packfile:
            return decoded

        info = packfile.read_animation_info()
        if info:
            decoded.class_name = info.class_name
            decoded.animation_type = info.animation_type
            decoded.duration = info.duration
            decoded.num_transform_tracks = info.num_transform_tracks
            decoded.num_float_tracks = info.num_float_tracks
            decoded.num_frames = info.num_frames

        skel = packfile.read_skeleton()
        bone_names = None
        if skel and skel.names:
            bone_names = skel.names
        elif fallback_bone_names:
            bone_names = fallback_bone_names

        decoded.buffer = packfile.read_full_animation(bone_names=bone_names)
        if decoded.buffer is not None:
            decoded.duration = decoded.buffer.duration
            decoded.num_frames = decoded.buffer.numFrames
            decoded.num_transform_tracks = len(decoded.buffer.bones)

        return decoded

    @classmethod
    def scan_animation_blobs(cls, data):
        """Find all Havok packfile blobs in raw data and extract animation info.

        Returns a list of HavokAnimationInfo, one per blob that contains an
        animation object. The order matches the blob order in the file, which
        corresponds to the CR2W animation entry order.
        """
        results = []
        search_start = 0
        while True:
            offset = cls.find_magic(data, search_start)
            if offset < 0:
                break
            packfile = cls.from_data(data, offset)
            if packfile:
                info = packfile.read_animation_info()
                if info:
                    results.append(info)
                else:
                    # Append a placeholder so indices stay aligned
                    results.append(HavokAnimationInfo())
            else:
                results.append(HavokAnimationInfo())
            search_start = offset + 4
        return results

    @classmethod
    def scan_and_decode_animations(cls, data, fallback_bone_names=None):
        """Find all Havok packfile blobs and decode spline animations.

        Returns:
            list[HavokDecodedAnimation], aligned to blob order.
        """
        results = []
        search_start = 0

        while True:
            offset = cls.find_magic(data, search_start)
            if offset < 0:
                break

            packfile = cls.from_data(data, offset)
            decoded = cls._decode_packfile_animation(
                packfile,
                fallback_bone_names=fallback_bone_names,
            )
            results.append(decoded)
            search_start = offset + 4

        return results

    @classmethod
    def decode_animation_blob_at_index(cls, data, blob_index, fallback_bone_names=None):
        """Decode only one Havok animation blob by its zero-based blob index."""
        if blob_index is None or blob_index < 0:
            return None

        search_start = 0
        current_index = 0
        while True:
            offset = cls.find_magic(data, search_start)
            if offset < 0:
                return None

            if current_index == blob_index:
                packfile = cls.from_data(data, offset)
                return cls._decode_packfile_animation(
                    packfile,
                    fallback_bone_names=fallback_bone_names,
                )

            current_index += 1
            search_start = offset + 4


class HavokAnimationInfo:
    """Animation metadata extracted from a Havok animation object."""

    __slots__ = ('class_name', 'animation_type', 'duration',
                 'num_transform_tracks', 'num_float_tracks', 'num_frames')

    def __init__(self):
        self.class_name = ""
        self.animation_type = 0
        self.duration = 0.0
        self.num_transform_tracks = 0
        self.num_float_tracks = 0
        self.num_frames = 0

    def __repr__(self):
        return (f"HavokAnimationInfo(class={self.class_name!r}, "
                f"duration={self.duration:.3f}s, "
                f"tracks={self.num_transform_tracks}, "
                f"frames={self.num_frames})")


class HavokDecodedAnimation:
    """Metadata + optionally decoded animation buffer from a Havok blob."""

    __slots__ = (
        'class_name',
        'animation_type',
        'duration',
        'num_transform_tracks',
        'num_float_tracks',
        'num_frames',
        'buffer',
    )

    def __init__(self):
        self.class_name = ""
        self.animation_type = 0
        self.duration = 0.0
        self.num_transform_tracks = 0
        self.num_float_tracks = 0
        self.num_frames = 0
        self.buffer = None

