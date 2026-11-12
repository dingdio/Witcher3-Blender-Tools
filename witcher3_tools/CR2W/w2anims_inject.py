"""Inject .w2anims clips while preserving untouched chunks and existing indices."""
import datetime
import os
import shutil
import struct
import tempfile
import zlib

from . import anims_builder, cr2w_writer

MAGIC = 0x57325243
# Versions 158+ use the serialization-flags byte our encoder writes.
SUPPORTED_VERSIONS = range(158, 165)
HEADER_SIZE = 160
ROW_SIZES = (1, 8, 8, 16, 24, 24)  # strings, names, imports, properties, exports, buffers
ROOT_CLASS = "CSkeletalAnimationSet"
ANIMATIONS_TYPE = "array:2,0,ptr:CSkeletalAnimationSetEntry"
HANDLE_PROPS = ("animation", "animBuffer", "motionExtraction", "uncompressedMotionExtraction")

# Cooked clips omit editor-only data.
_COOKED_DROP = {
    "CSkeletalAnimation": {"useOwnBitwiseCompressionParams", "bitwiseCompressionPreset",
                           "bitwiseCompressionSettings", "skeleton", "uncompressedMotionExtraction"},
    "CAnimationBufferBitwiseCompressed": {"compressionPreset", "compressionSettings", "sourceDataSize"},
}


class InjectError(Exception):
    pass


def name_hash(name):
    """CNameHash: signed-byte FNV-1a including NUL; empty names hash to zero."""
    if not name:
        return 0
    h = 0x811C9DC5
    for c in name.encode("iso-8859-1") + b"\0":
        h = ((h ^ ((c - 256) & 0xFFFFFFFF if c > 127 else c)) * 0x01000193) & 0xFFFFFFFF
    return h


def _crc(data):
    return zlib.crc32(data) & 0xFFFFFFFF


def _header_crc(header):
    h = bytearray(header[:HEADER_SIZE])
    struct.pack_into("<I", h, 32, 0xDEADBEEF)
    return _crc(bytes(h))


class RawCR2W:
    """Read CR2W tables and validate layout and CRCs."""

    def __init__(self, data):
        if len(data) < HEADER_SIZE:
            raise InjectError("File is too small to be a CR2W file")
        (magic, self.version, self.flags, self.timestamp, self.build, self.objects_end,
         self.buffers_end, crc, self.num_chunks) = struct.unpack_from("<IIIQIIIII", data, 0)
        if magic != MAGIC:
            raise InjectError("Not a CR2W file (bad magic)")
        if self.version not in SUPPORTED_VERSIONS:
            raise InjectError(f"Unsupported CR2W version {self.version} (Witcher 3 files 158-164 are supported)")
        if _header_crc(data) != crc:
            raise InjectError("Header CRC mismatch - file is corrupted")
        self.data = data
        self.tables = [struct.unpack_from("<III", data, 40 + 12 * i) for i in range(10)]
        if any(t[1] for t in self.tables[6:]):
            raise InjectError("File has in-place data tables; not supported")

        pos = HEADER_SIZE
        blobs = []
        for i, (off, count, tcrc) in enumerate(self.tables[:6]):
            if not count:
                if off or tcrc:
                    raise InjectError(f"Empty table {i} has a non-zero offset/CRC")
                blobs.append(b"")
                continue
            if off != pos:
                raise InjectError(f"Table {i} is not in the standard position")
            blob = data[off:off + count * ROW_SIZES[i]]
            if len(blob) != count * ROW_SIZES[i] or _crc(blob) != tcrc:
                raise InjectError(f"Table {i} CRC mismatch - file is corrupted")
            blobs.append(blob)
            pos += len(blob)
        self.tables_end = pos
        self.strings, self.names_blob, self.imports_blob, self.props_blob, exports_b, buffers_b = blobs
        self.names = [struct.unpack_from("<II", self.names_blob, 8 * i) for i in range(len(self.names_blob) // 8)]
        self.imports = [struct.unpack_from("<IHH", self.imports_blob, 8 * i) for i in range(len(self.imports_blob) // 8)]
        self.exports = [list(struct.unpack_from("<HHIIIiI", exports_b, 24 * i)) for i in range(len(exports_b) // 24)]
        self.buffers = [list(struct.unpack_from("<IIIIII", buffers_b, 24 * i)) for i in range(len(buffers_b) // 24)]

        # REDkit tolerates stale CRCs from blanked author/import strings.
        self.stale_crcs = []
        cur = self.tables_end
        for i, e in enumerate(self.exports):
            if e[4] != cur:
                raise InjectError(f"Chunk {i} is not contiguous")
            if e[6] and _crc(data[e[4]:e[4] + e[3]]) != e[6]:
                self.stale_crcs.append(i)
            cur += e[3]
        if cur != self.objects_end:
            raise InjectError("Object data end does not match the header")
        for i, b in enumerate(self.buffers):
            if b[1] != i + 1:
                raise InjectError("Buffer table indices are not sequential")
            if b[2]:
                if b[2] != cur or _crc(data[cur:cur + b[3]]) != b[5]:
                    raise InjectError(f"Embedded buffer {i + 1} is misplaced or corrupted")
                cur += b[3]
        if cur != len(data) or self.buffers_end != len(data):
            raise InjectError("File has trailing data or a wrong size in the header")

    def name(self, idx):
        return self.string_at(self.names[idx][0])

    def string_at(self, off):
        end = self.strings.index(b"\0", off)
        return self.strings[off:end].decode("iso-8859-1")

    def chunk(self, idx):
        e = self.exports[idx]
        return self.data[e[4]:e[4] + e[3]]

    def chunk_class(self, idx):
        return self.name(self.exports[idx][0])

    def buffer_data(self, index):
        b = self.buffers[index - 1]
        return self.data[b[2]:b[2] + b[3]] if b[2] else None

    def top_props(self, idx):
        """Map names to (type, value offset, bytes); '' holds the chunk tail."""
        chunk = self.chunk(idx)
        if not chunk or chunk[0] != 0:
            raise InjectError(f"Chunk {idx} has an unexpected property block")
        props, o = {}, 1
        try:
            while True:
                n = struct.unpack_from("<H", chunk, o)[0]
                if n == 0:
                    props[""] = (None, o + 2, chunk[o + 2:])
                    return props
                t, size = struct.unpack_from("<HI", chunk, o + 2)
                if size < 4 or o + 4 + size > len(chunk):
                    raise ValueError
                props[self.name(n)] = (self.name(t), o + 8, chunk[o + 8:o + 4 + size])
                o += 4 + size
        except (struct.error, IndexError, ValueError, UnicodeDecodeError):
            raise InjectError(f"Chunk {idx} ({self.chunk_class(idx)}) could not be read") from None


def _ptr(value_bytes):
    return struct.unpack("<i", value_bytes[:4])[0] - 1


def _handles(value_bytes):
    count = struct.unpack_from("<I", value_bytes, 0)[0]
    return [v - 1 for v in struct.unpack_from(f"<{count}i", value_bytes, 4)]


def describe_set(raw):
    """Return clip metadata and chunk indices in set order."""
    if not raw.exports or raw.chunk_class(0) != ROOT_CLASS:
        raise InjectError("First chunk is not a CSkeletalAnimationSet")
    root = raw.top_props(0)
    if root.get("animations", (None,))[0] != ANIMATIONS_TYPE:
        raise InjectError("Animation set has no 'animations' array")
    clips = []
    for entry_idx in _handles(root["animations"][2]):
        entry = raw.top_props(entry_idx)
        anim_idx = _ptr(entry["animation"][2])
        anim = raw.top_props(anim_idx)
        tail = entry[""][2]
        clip = {
            "name": raw.name(struct.unpack("<H", anim["name"][2][:2])[0]),
            "entry": entry_idx, "anim": anim_idx, "buffer": None, "bones": None, "buffer_index": 0,
            "me": _ptr(anim["motionExtraction"][2]) if "motionExtraction" in anim else None,
            "ume": _ptr(anim["uncompressedMotionExtraction"][2]) if "uncompressedMotionExtraction" in anim else None,
            "duration": struct.unpack("<f", anim["duration"][2][:4])[0] if "duration" in anim else 0.0,
            "events": struct.unpack_from("<I", tail, 0)[0] if len(tail) >= 4 else 0,
            "groups": "eventsGroupsRanges" in entry,
            "uncooked": False,
        }
        if clip["me"] is not None and clip["me"] < 0:
            clip["me"] = None
        if clip["ume"] is not None and clip["ume"] < 0:
            clip["ume"] = None
        if "animBuffer" in anim:
            clip["buffer"] = _ptr(anim["animBuffer"][2])
            buf = raw.top_props(clip["buffer"])
            if "bones" in buf:
                clip["bones"] = struct.unpack_from("<I", buf["bones"][2], 0)[0]
            if "deferredData" in buf:
                clip["buffer_index"] = struct.unpack("<H", buf["deferredData"][2][:2])[0]
            clip["uncooked"] = "sourceDataSize" in buf
        clips.append(clip)
    return clips


def set_is_uncooked(raw, clips=None):
    """Detect editor source data or embedded buffers."""
    clips = describe_set(raw) if clips is None else clips
    return any(c["uncooked"] for c in clips) or any(b[2] for b in raw.buffers)


def _remap_handles(chunk, mapping):
    for prop in chunk.PROPS or []:
        if prop.theName not in HANDLE_PROPS:
            continue
        for h in getattr(prop, "Handles", None) or []:
            if getattr(h, "ChunkHandle", False) and h.Reference is not None:
                if h.Reference not in mapping:
                    raise InjectError("Internal error: unmapped chunk handle")
                h.Reference = h.val = mapping[h.Reference]


class _Clip:
    """Build a clip before assigning final chunk indices."""

    def __init__(self, spec, uncooked, preserve_events=False):
        self.spec = spec
        self.cr2w = anims_builder._init_cr2w()
        events = None if preserve_events else spec.get('entry_events')
        if events is not None:
            events = [{**event, 'animation_name': spec['action_name']} for event in events]
        result = anims_builder._build_animation_chunks(
            self.cr2w,
            action_name=spec["action_name"],
            bones=spec["bones"],
            tracks=spec.get("tracks"),
            num_frames=spec["num_frames"],
            dt=spec.get("dt", anims_builder.DEFAULT_DT),
            fps=spec.get("fps", 30.0),
            buffer_index=1,
            skeletal_type=spec.get("skeletal_type", "SAT_Normal"),
            additive_type=spec.get("additive_type"),
            motion_extraction=spec.get("motion_extraction"),
            entry_events=events,
        )
        self.payload = result["buffer_payload"]
        self.chunks = list(self.cr2w.CHUNKS.CHUNKS)
        if not uncooked:
            self.chunks = [c for c in self.chunks if c.Type != "CUncompressedMotionExtraction"]
            for chunk in self.chunks:
                drop = _COOKED_DROP.get(chunk.Type)
                if drop:
                    chunk.PROPS = [p for p in chunk.PROPS if p.theName not in drop]
                if chunk.Type == "CSkeletalAnimation":
                    chunk.postPropsData = struct.pack("<I", 0)
                    chunk.embeddedAnimData = None
        self.by_type = {c.Type: (i, c) for i, c in enumerate(self.cr2w.CHUNKS.CHUNKS) if c in self.chunks}

    def set_buffer_index(self, index):
        _, buf = self.by_type["CAnimationBufferBitwiseCompressed"]
        for prop in buf.PROPS:
            if prop.theName == "deferredData":
                prop.ValueA = index


def _plan(raw, clips, specs):
    """Validate specs; return (spec, replaced clip or None) pairs."""
    by_name = {c["name"].lower(): c for c in clips}
    seen, plan = set(), []
    bone_counts = {c["bones"] for c in clips if c["bones"] is not None}
    expected = next(iter(bone_counts)) if len(bone_counts) == 1 else None
    references = set()
    ext = raw.top_props(0).get('extAnimEvents')
    if ext:
        for handle in _handles(ext[2]):
            if handle < -1:
                references.add(raw.string_at(raw.imports[-handle - 2][0]).replace('/', '\\').lower())
    for spec in specs:
        target = spec.get("replace")
        if target:
            old = by_name.get(target.lower())
            if old is None:
                raise InjectError(f"Clip '{target}' to replace is not in the set")
            if old["buffer"] is None:
                raise InjectError(f"Clip '{target}' has no bitwise animation buffer; cannot replace it")
            # Cooked group ranges index the old events array; the engine reads past a shorter one.
            if spec.get("replace_events") and old["groups"]:
                raise InjectError(f"Clip '{target}' has cooked event groups, so its events cannot be replaced; "
                                  "replace it in the uncooked REDkit set instead")
            spec = dict(spec, action_name=old["name"])
            need = old["bones"] if old["bones"] is not None else expected
        else:
            old = None
            if spec["action_name"].lower() in by_name:
                raise InjectError(f"Clip '{spec['action_name']}' already exists in the set; choose it as "
                                  "the clip to replace, or rename the action")
            need = expected
        key = spec["action_name"].lower()
        if key in seen:
            raise InjectError(f"Clip '{spec['action_name']}' is targeted twice")
        seen.add(key)
        if need is not None and len(spec["bones"]) != need:
            raise InjectError(
                f"Clip '{spec['action_name']}' has {len(spec['bones'])} bones but the set expects {need}; "
                "export it from an armature built from this set's rig")
        retained = set()
        for source, info in spec.get('event_sources', {}).items():
            if source.replace('/', '\\').lower() not in references:
                continue
            if old is None and spec['action_name'] != info['animation']:
                continue
            if info['changed'] or spec['action_name'] != info['animation']:
                raise InjectError(f"Linked events belong to '{source}'. Export a standalone .w2anims for these event edits; replacing this clip would also retain the linked events")
            retained.add(source)
        if retained:
            spec = dict(spec, entry_events=[e for e in spec.get('entry_events', []) if e.get('source_file', '') not in retained])
        plan.append((spec, old))
    return plan


def inject_bytes(src_data, specs, embed_buffers=False, companions=None):
    """Return (file bytes, external buffers by index, report).

    Specs with "replace" keep the existing entry and name. Embedding requires companions by index.
    """
    if not specs:
        raise InjectError("No clips to inject")
    raw = RawCR2W(src_data)
    clips = describe_set(raw)
    plan = _plan(raw, clips, specs)
    out, external, report = _splice(raw, clips, plan, embed_buffers, companions or {})
    verify_injection(src_data, out, plan, external, report, companions or {})
    return out, external, report


def _splice(raw, clips, plan, embed_buffers=False, companions=None):
    uncooked = set_is_uncooked(raw, clips)
    companions = companions or {}
    # Loose REDkit files require embedded buffers.
    embed_buffers = bool(embed_buffers or uncooked)
    n = len(raw.exports)
    next_chunk = n
    next_buffer = len(raw.buffers) + 1
    replaced_chunks = {}      # chunk index -> W_CLASS
    appended = []             # final chunk order
    entries_added = []
    buffer_payloads = {}      # buffer index -> payload
    orphaned = []
    built_names = []

    for spec, old in plan:
        preserve_events = old is not None and not spec.get('replace_events', False)
        clip = _Clip(spec, uncooked, preserve_events=preserve_events)
        built_names.append(clip.cr2w)
        mapping = {}
        for i, c in enumerate(clip.cr2w.CHUNKS.CHUNKS):
            if c not in clip.chunks:
                continue
            reuse = None
            if old is not None:
                reuse = {"CSkeletalAnimationSetEntry": old["entry"], "CSkeletalAnimation": old["anim"],
                         "CAnimationBufferBitwiseCompressed": old["buffer"],
                         "CLineMotionExtraction2": old["me"], "CUncompressedMotionExtraction": old["ume"]}.get(c.Type)
            if reuse is None:
                mapping[i] = next_chunk
                next_chunk += 1
            else:
                mapping[i] = reuse
        for c in clip.chunks:
            _remap_handles(c, mapping)
        if old is not None and old["buffer_index"]:
            index = old["buffer_index"]
        else:
            index = next_buffer
            next_buffer += 1
        clip.set_buffer_index(index)
        buffer_payloads[index] = clip.payload
        for i, c in enumerate(clip.cr2w.CHUNKS.CHUNKS):
            if c not in clip.chunks:
                continue
            final = mapping[i]
            if preserve_events and c.Type == "CSkeletalAnimationSetEntry":
                continue  # Preserve entry events and references.
            if final < n:
                replaced_chunks[final] = c
            else:
                appended.append(c)
        if old is None:
            _, entry = clip.by_type["CSkeletalAnimationSetEntry"]
            entries_added.append(mapping[clip.cr2w.CHUNKS.CHUNKS.index(entry)])
        else:
            used = set(mapping.values())
            orphaned += [i for i in (old["me"], old["ume"]) if i is not None and i not in used]

    names = [raw.name(i) for i in range(len(raw.names))]
    name_to_index = {}
    for i, nm in enumerate(names):
        name_to_index.setdefault(nm, i)
    imports_key = {}
    for i, (path_off, cls, flags) in enumerate(raw.imports):
        path = None if flags & 8 else raw.string_at(path_off)
        imports_key.setdefault((names[cls], path, flags), i)

    strings = bytearray(raw.strings)
    new_names, new_imports = bytearray(), bytearray()

    def add_string(s):
        off = len(strings)
        strings.extend(s.encode("iso-8859-1") + b"\0")
        return off

    import_index = {}
    for cr2w in built_names:
        collected_names, collected_imports = cr2w_writer._collect_names_and_imports(cr2w)
        for nm in collected_names:
            if nm not in name_to_index:
                name_to_index[nm] = len(names)
                names.append(nm)
                new_names += struct.pack("<II", add_string(nm), name_hash(nm))
        for imp in collected_imports:
            key = (imp.class_name, imp.path, imp.flags)
            if key not in imports_key:
                imports_key[key] = len(raw.imports) + len(new_imports) // 8
                new_imports += struct.pack("<IHH", add_string(imp.path), name_to_index[imp.class_name], imp.flags)
            import_index[key] = imports_key[key]

    def encode(chunk):
        if chunk.Type not in name_to_index:
            raise InjectError(f"Internal error: class name '{chunk.Type}' missing from names")
        return name_to_index[chunk.Type], cr2w_writer._encode_chunk(chunk, name_to_index, import_index)

    root = raw.chunk(0)
    if entries_added:
        _, val_off, val = raw.top_props(0)["animations"]
        count = struct.unpack_from("<I", val, 0)[0]
        new_val = (struct.pack("<I", count + len(entries_added)) + val[4:]
                   + b"".join(struct.pack("<i", i + 1) for i in entries_added))
        root = root[:val_off - 4] + struct.pack("<I", 4 + len(new_val)) + new_val + root[val_off + len(val):]

    export_rows = [list(e) for e in raw.exports]
    payloads = [root] + [raw.chunk(i) for i in range(1, n)]
    for idx, chunk in replaced_chunks.items():
        export_rows[idx][0], payloads[idx] = encode(chunk)
        if chunk.Type == 'CSkeletalAnimationSetEntry':
            end = raw.top_props(idx)[''][1]
            tail = cr2w_writer._encode_variant_size_type_array(chunk.postPropsVariantProps, name_to_index, import_index)
            payloads[idx] = raw.chunk(idx)[:end] + tail
    for chunk in appended:
        cls, payload = encode(chunk)
        export_rows.append([cls, 0, 0, 0, 0, 0, 0])
        payloads.append(payload)
    if len(payloads) != next_chunk:
        raise InjectError("Internal error: chunk index bookkeeping drifted")

    buffer_rows = [list(b) for b in raw.buffers]
    for index in range(len(raw.buffers) + 1, next_buffer):
        buffer_rows.append([0, index, 0, 0, 0, 0])
    buffer_data = {}
    for row in buffer_rows:
        index = row[1]
        if index in buffer_payloads:
            data = buffer_payloads[index]
            row[3] = row[4] = len(data)
            row[5] = _crc(data)
        else:
            data = raw.buffer_data(index) if index <= len(raw.buffers) else None
            if data is None and embed_buffers:
                data = companions.get(index)
                if data is None:
                    raise InjectError(f"Buffer {index} is stored in a separate .{index}.buffer file that was not "
                                      "found; it is needed to embed the buffers")
                if len(data) != row[3] or _crc(data) != row[5]:
                    raise InjectError(f"External buffer {index} does not match the buffer table (stale file)")
        buffer_data[index] = data
    embedded = [r for r in buffer_rows if embed_buffers or raw_offset(raw, r[1])]

    blobs = [bytes(strings), raw.names_blob + bytes(new_names), raw.imports_blob + bytes(new_imports),
             raw.props_blob, None, None]
    counts = [len(blobs[0]), len(names), len(raw.imports) + len(new_imports) // 8,
              len(raw.props_blob) // 16, len(export_rows), len(buffer_rows)]
    cur = HEADER_SIZE + sum(c * r for c, r in zip(counts, ROW_SIZES))
    touched = set(replaced_chunks) | ({0} if entries_added else set())
    for i, (row, payload) in enumerate(zip(export_rows, payloads)):
        row[3], row[4] = len(payload), cur
        if i >= n or i in touched:
            row[6] = _crc(payload)
        cur += len(payload)
    objects_end = cur
    for row in buffer_rows:
        row[2] = 0
    for row in embedded:
        row[2] = cur
        cur += row[3]
    blobs[4] = b"".join(struct.pack("<HHIIIiI", *r) for r in export_rows)
    blobs[5] = b"".join(struct.pack("<IIIIII", *r) for r in buffer_rows)

    table_rows, pos = [], HEADER_SIZE
    for blob, count in zip(blobs, counts):
        if count:
            table_rows.append((pos, count, _crc(blob)))
            pos += len(blob)
        else:
            table_rows.append((0, 0, 0))
    table_rows += [(0, 0, 0)] * 4

    header = bytearray(struct.pack("<IIIQIIIII", MAGIC, raw.version, raw.flags, raw.timestamp, raw.build,
                                   objects_end, cur, 0, raw.num_chunks))
    for row in table_rows:
        header += struct.pack("<III", *row)
    struct.pack_into("<I", header, 32, _header_crc(header))

    out = b"".join([bytes(header)] + blobs + payloads + [buffer_data[r[1]] for r in embedded])
    external = {} if embed_buffers else {i: p for i, p in buffer_payloads.items() if not raw_offset(raw, i)}
    report = {
        "added": [s["action_name"] for s, old in plan if old is None],
        "replaced": [s["action_name"] for s, old in plan if old is not None],
        "replaced_chunks": sorted(replaced_chunks),
        "replaced_buffers": sorted(i for i in buffer_payloads if i <= len(raw.buffers)),
        "orphaned_chunks": orphaned,
        "uncooked": uncooked,
        "embedded": embed_buffers,
        "stale_crcs_kept": [i for i in raw.stale_crcs if i not in replaced_chunks],
    }
    return out, external, report


def raw_offset(raw, index):
    return raw.buffers[index - 1][2] if index <= len(raw.buffers) else 0


def verify_injection(src_data, out_data, plan, external, report, companions):
    """Verify preserved bytes, clip indices and new buffer CRCs."""
    src, out = RawCR2W(src_data), RawCR2W(out_data)

    def check(ok, what):
        if not ok:
            raise InjectError(f"Verification failed: {what}")

    check((src.version, src.flags, src.timestamp, src.build, src.num_chunks)
          == (out.version, out.flags, out.timestamp, out.build, out.num_chunks), "header fields changed")
    rewritten = set(report["replaced_chunks"]) | ({0} if report["added"] else set())
    check(set(out.stale_crcs) == set(src.stale_crcs) - rewritten, "chunk CRCs")
    check(out.strings.startswith(src.strings), "string table prefix changed")
    check(out.names[:len(src.names)] == src.names, "existing names changed")
    check(all(out.names[i][1] == name_hash(out.name(i)) for i in range(len(src.names), len(out.names))),
          "new name hash")
    check(out.imports[:len(src.imports)] == src.imports, "existing imports changed")
    check(out.props_blob == src.props_blob, "property table changed")

    n = len(src.exports)
    replaced = set(report["replaced_chunks"])
    for i in range(1, n):
        a, b = src.exports[i], out.exports[i]
        if i in replaced:
            check(a[1:3] + a[5:6] == b[1:3] + b[5:6], f"replaced chunk {i} row flags changed")
        else:
            check(a[:4] + a[5:] == b[:4] + b[5:] and src.chunk(i) == out.chunk(i), f"chunk {i} changed")
    check(src.exports[0][:3] + src.exports[0][5:6] == out.exports[0][:3] + out.exports[0][5:6], "root export row changed")

    src_root, out_root = src.top_props(0), out.top_props(0)
    check([k for k in src_root if k] == [k for k in out_root if k], "root property list changed")
    for key, (t, _, v) in src_root.items():
        if key not in ("animations", ""):
            check(out_root[key][0] == t and out_root[key][2] == v, f"root property '{key}' changed")
    src_handles, out_handles = _handles(src_root["animations"][2]), _handles(out_root["animations"][2])
    check(out_handles[:len(src_handles)] == src_handles, "existing animation entries changed")
    added = out_handles[len(src_handles):]
    check(len(added) == len(report["added"]), "wrong number of new entries")
    check(all(i >= n and out.chunk_class(i) == "CSkeletalAnimationSetEntry" for i in added), "new entries are not new chunks")
    if not added:
        check(src.chunk(0) == out.chunk(0), "root chunk changed")
    else:
        _, sv_off, sv = src_root["animations"]
        _, ov_off, ov = out_root["animations"]
        check(out.chunk(0)[:sv_off - 4] == src.chunk(0)[:sv_off - 4]
              and out.chunk(0)[ov_off + len(ov):] == src.chunk(0)[sv_off + len(sv):]
              and ov[4:len(sv)] == sv[4:], "root bytes outside the new entries changed")

    replaced_buffers = set(report["replaced_buffers"])
    for i, a in enumerate(src.buffers):
        b = out.buffers[i]
        index = i + 1
        if index in replaced_buffers:
            continue
        check(a[:2] + a[3:] == b[:2] + b[3:], f"buffer {index} row changed")
        before = src.buffer_data(index) if a[2] else companions.get(index)
        if b[2]:
            check(before is not None and out.buffer_data(index) == before, f"buffer {index} data changed")
    for b in out.buffers:
        index = b[1]
        if index in replaced_buffers or index > len(src.buffers):
            data = out.buffer_data(index) if b[2] else external.get(index)
            check(data is not None and len(data) == b[3] and _crc(data) == b[5], f"new buffer {index}")

    src_clips, out_clips = describe_set(src), describe_set(out)
    check([c["name"] for c in out_clips] == [c["name"] for c in src_clips] + report["added"], "clip names")
    by_name = {c["name"]: c for c in out_clips}
    for spec, old in plan:
        clip = by_name[spec["action_name"]]
        check(clip["bones"] == len(spec["bones"]), f"clip '{spec['action_name']}' bone count")
        if old is not None:
            check((clip["entry"], clip["anim"], clip["buffer"]) == (old["entry"], old["anim"], old["buffer"]),
                  f"replaced clip '{old['name']}' moved")
            if spec.get('replace_events', False):
                end = src.top_props(old['entry'])[''][1]
                check(out.chunk(clip['entry'])[:end] == src.chunk(old['entry'])[:end], 'entry properties changed')
                check(clip['events'] == len(spec.get('entry_events', [])), 'edited event count')


def buffer_path(w2anims_path, index):
    return f"{w2anims_path}.{index}.buffer"


def read_companions(path, raw):
    """Read and validate external .N.buffer files."""
    found, missing = {}, []
    for b in raw.buffers:
        if b[2]:
            continue
        p = buffer_path(path, b[1])
        if not os.path.isfile(p):
            missing.append(b[1])
            continue
        with open(p, "rb") as f:
            data = f.read()
        if len(data) != b[3] or _crc(data) != b[5]:
            raise InjectError(f"{os.path.basename(p)} does not match the buffer table (stale or corrupted)")
        found[b[1]] = data
    return found, missing


def backup_file(path, backup_dir):
    """Back up the set and companion buffers; return the timestamped folder."""
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs(backup_dir, exist_ok=True)
    folder = tempfile.mkdtemp(prefix=f"{os.path.basename(path)}.{stamp}.", dir=backup_dir)
    base = os.path.normcase(os.path.basename(path))
    shutil.copy2(path, os.path.join(folder, os.path.basename(path)))
    for name in os.listdir(os.path.dirname(path) or "."):
        key = os.path.normcase(name)
        if key.startswith(base + ".") and key.endswith(".buffer"):
            shutil.copy2(os.path.join(os.path.dirname(path), name), os.path.join(folder, name))
    return folder


def _write_files(pending):
    """Stage writes on the destination volume and restore originals on failure."""
    folder = os.path.dirname(pending[0][0]) or "."
    os.makedirs(folder, exist_ok=True)
    scratch = tempfile.mkdtemp(prefix=".w3_inject_", dir=folder)
    originals = []
    keep_backup = False
    try:
        for i, (path, data) in enumerate(pending):
            tmp = os.path.join(scratch, f"new_{i}")
            with open(tmp, "wb") as f:
                f.write(data)
            with open(tmp, "rb") as f:
                if f.read() != data:
                    raise InjectError(f"Read-back mismatch writing {path}")
        for i, (path, _) in enumerate(pending):
            backup = None
            if os.path.exists(path):
                backup = os.path.join(scratch, os.path.basename(path))
                os.replace(path, backup)
            originals.append((path, backup))
            os.replace(os.path.join(scratch, f"new_{i}"), path)
    except BaseException as exc:
        # Keep recovery files if rollback is interrupted too.
        keep_backup = True
        failed = False
        for path, backup in reversed(originals):
            try:
                if backup is not None:
                    os.replace(backup, path)
                elif os.path.exists(path):
                    os.remove(path)
            except OSError:
                failed = True
        if failed:
            raise InjectError(f"Write failed: {exc}; recovery files remain in {scratch}") from exc
        keep_backup = False
        raise
    finally:
        if not keep_backup:
            # The write is already committed or rolled back; cleanup must not turn it into a failure.
            shutil.rmtree(scratch, ignore_errors=True)


def inject_file(src_path, out_path, specs, embed_buffers=None, backup_dir=None):
    """Write injected clips and return a report; in-place edits require backup_dir."""
    src_path, out_path = os.path.abspath(src_path), os.path.abspath(out_path)
    in_place = (os.path.normcase(os.path.realpath(src_path)) == os.path.normcase(os.path.realpath(out_path))
                or (os.path.exists(out_path) and os.path.samefile(src_path, out_path)))
    if in_place and not backup_dir:
        raise InjectError("Output must be a different file than the source .w2anims")
    with open(src_path, "rb") as f:
        src_data = f.read()
    raw = RawCR2W(src_data)
    companions, missing = read_companions(src_path, raw)
    out_data, external, report = inject_bytes(src_data, specs, bool(embed_buffers), companions)
    embed_buffers = report["embedded"]

    pending = [(buffer_path(out_path, i), p) for i, p in external.items()]
    copied = []
    if not embed_buffers and not in_place:
        copied = [(i, d) for i, d in companions.items() if i not in external]
        pending += [(buffer_path(out_path, i), d) for i, d in copied]

    backup = backup_file(src_path, backup_dir) if in_place else None
    # Publish the header only after its buffers are ready.
    _write_files(pending + [(out_path, out_data)])

    report.update({
        "output": out_path,
        "existing_clips": len(describe_set(raw)),
        "new_buffer_files": sorted(external),
        "copied_buffers": len(copied),
        "missing_buffers": [] if embed_buffers else missing,
        "backup": backup,
    })
    return report


def _workspace_bytes(src_path):
    with open(src_path, "rb") as f:
        src_data = f.read()
    raw = RawCR2W(src_data)
    clips = describe_set(raw)
    companions, missing = read_companions(src_path, raw)
    if missing:
        raise InjectError(f"{len(missing)} .buffer files next to the source are missing; extract them too")
    out, _, report = _splice(raw, clips, [], embed_buffers=True, companions=companions)
    verify_injection(src_data, out, [], {}, report, companions)
    return out


def copy_into_workspace(src_path, dst_path):
    """Copy a set with embedded buffers, as loose REDkit files require."""
    if os.path.exists(dst_path):
        raise InjectError(f"{dst_path} already exists in the workspace")
    _write_files([(dst_path, _workspace_bytes(src_path))])
    return dst_path


def rebuild_workspace(src_path, dst_path, backup_dir):
    """Validate the depot copy before replacing the workspace set."""
    data = _workspace_bytes(src_path)
    backup = backup_file(dst_path, backup_dir)
    _write_files([(dst_path, data)])
    folder, base = os.path.dirname(dst_path), os.path.basename(dst_path)
    leftover = []
    for name in os.listdir(backup):
        if name.startswith(base + ".") and name.endswith(".buffer"):
            try:
                os.remove(os.path.join(folder, name))
            except OSError:
                leftover.append(name)
    return {"backup": backup, "leftover_buffers": leftover}
