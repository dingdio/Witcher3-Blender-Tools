import io
import os
import struct
import zlib

from .CR2W_helpers import Enums
from .CR2W_types import CVariantSizeNameType, PROPERTY
from .Types.VariousTypes import CNAME_INDEX, NAME, CMatrix4x4, CPaddedBuffer

MAGIC = 0x57325243
DEADBEEF = 0xDEADBEEF


def _ensure_parent_dir(file_path):
    """Ensure the parent directory of file_path exists."""
    parent = os.path.dirname(file_path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)


def write_w2mesh(cr2w, file_path):
    _ensure_parent_dir(file_path)
    data = _build_cr2w_bytes(cr2w)
    with open(file_path, "wb") as f:
        f.write(data)


def write_w2anims(cr2w, file_path):
    _ensure_parent_dir(file_path)
    data = _build_cr2w_bytes(cr2w)
    with open(file_path, "wb") as f:
        f.write(data)


def write_w2cutscene(cr2w, file_path):
    _ensure_parent_dir(file_path)
    data = _build_cr2w_bytes(cr2w)
    with open(file_path, "wb") as f:
        f.write(data)


def write_xbm(cr2w, file_path):
    _ensure_parent_dir(file_path)
    data = _build_cr2w_bytes(cr2w)
    with open(file_path, "wb") as f:
        f.write(data)


def _build_cr2w_bytes(cr2w):
    names, imports = _collect_names_and_imports(cr2w)
    name_to_index = {name: idx for idx, name in enumerate(names)}

    string_table = _StringTable()
    for name in names:
        string_table.add(name)
    for import_entry in imports:
        string_table.add(import_entry.path)

    string_bytes = string_table.bytes()

    import_index = {
        (imp.class_name, imp.path, imp.flags): idx
        for idx, imp in enumerate(imports)
    }

    # Build chunk data and export entries
    data_stream = io.BytesIO()
    exports = []
    chunks = getattr(cr2w.CHUNKS, "CHUNKS", [])
    exports_src = getattr(cr2w, "CR2WExport", [])
    for idx, chunk in enumerate(chunks):
        export_src = exports_src[idx] if idx < len(exports_src) else None
        chunk_bytes = _encode_chunk(chunk, name_to_index, import_index)
        data_offset = data_stream.tell()
        data_stream.write(chunk_bytes)
        data_size = len(chunk_bytes)
        exports.append(
            _ExportEntry(
                class_name=name_to_index.get(chunk.Type, 0),
                object_flags=getattr(export_src, "objectFlags", 0),
                parent_id=getattr(export_src, "parentID", 0),
                data_size=data_size,
                data_offset=data_offset,
                template=getattr(export_src, "template", 0),
                crc32=_crc32(chunk_bytes),
            )
        )

    file_size_rel = data_stream.tell()

    # Build buffers
    buffers = []
    buffer_src = getattr(cr2w, "CR2WBuffer", [])
    buffer_data = getattr(cr2w, "BufferData", [])
    for idx, buf in enumerate(buffer_src):
        data = buffer_data[idx] if idx < len(buffer_data) else b""
        buf_offset = data_stream.tell()
        data_stream.write(data)
        buf_size = len(data)
        buffers.append(
            _BufferEntry(
                flags=getattr(buf, "flags", 0),
                index=getattr(buf, "index", idx + 1),
                offset=buf_offset,
                disk_size=buf_size,
                mem_size=getattr(buf, "memSize", buf_size),
                crc32=_crc32(data) if buf_size else 0,
            )
        )

    buffer_size_rel = data_stream.tell()

    # Build tables
    names_bytes = _build_names_table(names, string_table)
    imports_bytes = _build_imports_table(imports, name_to_index, string_table)
    props_bytes = _build_props_table(getattr(cr2w, "CR2W_Property", []))

    # Compute table offsets
    string_offset = 160
    header_size = string_offset + len(string_bytes)
    pos = header_size

    names_offset = pos
    pos += len(names_bytes)

    imports_offset = pos if imports else 0
    pos += len(imports_bytes)

    props_offset = pos
    pos += len(props_bytes)

    exports_bytes = _build_exports_table(exports, data_offset_base=pos)
    exports_offset = pos
    pos += len(exports_bytes)

    buffers_bytes = _build_buffers_table(buffers, data_offset_base=pos)
    buffers_offset = pos if buffers else 0
    pos += len(buffers_bytes)

    data_offset = pos

    # Update export/buffer offsets with data offset
    exports_bytes = _build_exports_table(exports, data_offset_base=data_offset)
    buffers_bytes = _build_buffers_table(buffers, data_offset_base=data_offset)

    file_size = file_size_rel + data_offset
    buffer_size = buffer_size_rel + data_offset

    # Table headers
    table_headers = [
        _TableHeader(string_offset, len(string_bytes), _crc32(string_bytes)),
        _TableHeader(names_offset, len(names), _crc32(names_bytes)),
        _TableHeader(imports_offset, len(imports), _crc32(imports_bytes)),
        _TableHeader(props_offset, len(getattr(cr2w, "CR2W_Property", [])), _crc32(props_bytes)),
        _TableHeader(exports_offset, len(exports), _crc32(exports_bytes)),
        _TableHeader(buffers_offset, len(buffers), _crc32(buffers_bytes)),
    ]
    while len(table_headers) < 10:
        table_headers.append(_TableHeader(0, 0, 0))

    # File header
    version = getattr(cr2w.HEADER, "version", 162)
    flags = getattr(cr2w.HEADER, "flags", 0)
    timestamp = getattr(cr2w.HEADER, "timestamp", 0)
    build_version = getattr(cr2w.HEADER, "buildVersion", 1150341)
    num_chunks = len(exports)

    header_crc = _calc_header_crc(
        version,
        flags,
        timestamp,
        build_version,
        file_size,
        buffer_size,
        num_chunks,
        table_headers,
    )

    # Assemble final file
    out = io.BytesIO()
    out.write(struct.pack("<I", MAGIC))
    out.write(struct.pack("<I", version))
    out.write(struct.pack("<I", flags))
    out.write(struct.pack("<Q", timestamp))
    out.write(struct.pack("<I", build_version))
    out.write(struct.pack("<I", file_size))
    out.write(struct.pack("<I", buffer_size))
    out.write(struct.pack("<I", header_crc))
    out.write(struct.pack("<I", num_chunks))

    for th in table_headers:
        out.write(struct.pack("<III", th.offset, th.count, th.crc32))

    out.write(string_bytes)
    out.write(names_bytes)
    out.write(imports_bytes)
    out.write(props_bytes)
    out.write(exports_bytes)
    out.write(buffers_bytes)
    out.write(data_stream.getvalue())

    return out.getvalue()


class _StringTable:
    def __init__(self):
        self._data = bytearray()
        self._offsets = {}
        self.add("")

    def add(self, value):
        if value is None:
            return None
        if value in self._offsets:
            return self._offsets[value]
        offset = len(self._data)
        self._data.extend(value.encode("iso-8859-1") + b"\x00")
        self._offsets[value] = offset
        return offset

    def offset(self, value):
        return self._offsets[value]

    def bytes(self):
        return bytes(self._data)


class _ImportEntry:
    def __init__(self, class_name, path, flags):
        self.class_name = class_name
        self.path = path
        self.flags = int(flags or 0)


class _ExportEntry:
    def __init__(self, class_name, object_flags, parent_id, data_size, data_offset, template, crc32):
        self.class_name = class_name
        self.object_flags = object_flags
        self.parent_id = parent_id
        self.data_size = data_size
        self.data_offset = data_offset
        self.template = template
        self.crc32 = crc32


class _BufferEntry:
    def __init__(self, flags, index, offset, disk_size, mem_size, crc32):
        self.flags = flags
        self.index = index
        self.offset = offset
        self.disk_size = disk_size
        self.mem_size = mem_size
        self.crc32 = crc32


class _TableHeader:
    def __init__(self, offset, count, crc32):
        self.offset = offset
        self.count = count
        self.crc32 = crc32


def _collect_names_and_imports(cr2w):
    names = [""]
    names_set = set(names)
    imports = []
    import_keys = {}

    def add_name(value):
        if value is None:
            return
        if value not in names_set:
            names_set.add(value)
            names.append(value)

    def add_import(class_name, path, flags):
        if not class_name or not path:
            return
        key = (class_name, path, int(flags or 0))
        if key in import_keys:
            return
        import_keys[key] = len(imports)
        imports.append(_ImportEntry(class_name, path, int(flags or 0)))
        add_name(class_name)

    # pre-existing imports
    for imp in getattr(cr2w, "CR2WImport", []) or []:
        class_name = imp.className
        if not isinstance(class_name, str):
            try:
                class_name = cr2w.CNAMES[class_name].name.value
            except Exception:
                class_name = None
        path = getattr(imp, "path", None) or getattr(imp, "depotPath", None)
        add_import(class_name, path, getattr(imp, "flags", 0))

    def handle_cname_value(obj):
        value = _get_cname_value(obj)
        if value:
            add_name(value)

    def handle_handles(handles):
        for h in handles:
            if not h or getattr(h, "ChunkHandle", False):
                continue
            add_import(getattr(h, "ClassName", None), getattr(h, "DepotPath", None), getattr(h, "Flags", 0))

    def collect_prop(prop):
        if prop is None:
            return
        add_name(getattr(prop, "theName", None))
        add_name(getattr(prop, "theType", None))

        p_type = getattr(prop, "theType", None)

        if p_type in Enums.Enum_Types or p_type in Enums.Enum_Flags_Types:
            enum_obj = getattr(prop, "Index", None)
            strings = []
            if enum_obj is not None:
                if hasattr(enum_obj, "strings"):
                    strings = enum_obj.strings
                elif hasattr(enum_obj, "String"):
                    strings = [enum_obj.String]
            if hasattr(prop, "strings"):
                strings = prop.strings
            for s in strings or []:
                add_name(s)

        if p_type == "CName":
            handle_cname_value(prop)

        if p_type == "TagList":
            for tag in getattr(prop, "TagList", None) or []:
                handle_cname_value(tag)

        if p_type and (p_type.startswith("handle:") or p_type.startswith("ptr:") or p_type.startswith("soft:")):
            handles = _get_handles(prop)
            handle_handles(handles)

        subprops = _get_subprops(prop)
        if subprops:
            for sp in subprops:
                collect_prop(sp)

        elements = getattr(prop, "elements", None)
        if elements:
            for el in elements:
                if isinstance(el, PROPERTY):
                    collect_prop(el)
                elif isinstance(el, CVariantSizeNameType):
                    if el.PROP:
                        collect_prop(el.PROP)
                elif isinstance(el, CNAME_INDEX):
                    handle_cname_value(el)
                elif isinstance(el, NAME):
                    handle_cname_value(el)
                else:
                    # handle arrays of handles
                    if hasattr(el, "ChunkHandle"):
                        handle_handles([el])

    chunks = getattr(cr2w.CHUNKS, "CHUNKS", [])
    for chunk in chunks:
        add_name(getattr(chunk, "Type", None))
        for prop in getattr(chunk, "PROPS", []) or []:
            collect_prop(prop)

        if hasattr(chunk, "CMesh"):
            bone_names = getattr(chunk.CMesh, "BoneNames", None)
            if bone_names:
                for bn in getattr(bone_names, "elements", []) or []:
                    handle_cname_value(bn)

        if hasattr(chunk, "CMaterialInstance"):
            params = getattr(chunk.CMaterialInstance, "InstanceParameters", None)
            if params:
                for el in getattr(params, "elements", []) or []:
                    if isinstance(el, CVariantSizeNameType) and el.PROP:
                        collect_prop(el.PROP)

    return names, imports


def _get_subprops(prop):
    if hasattr(prop, "MoreProps"):
        return prop.MoreProps
    if hasattr(prop, "More"):
        return prop.More
    if hasattr(prop, "PROPS"):
        return prop.PROPS
    return None


def _get_handles(prop):
    if hasattr(prop, "Handles") and prop.Handles:
        return prop.Handles
    if hasattr(prop, "elements") and prop.elements:
        if all(hasattr(el, "ChunkHandle") for el in prop.elements):
            return prop.elements
    if hasattr(prop, "ChunkHandle"):
        return [prop]
    return []


def _get_cname_value(obj):
    if obj is None:
        return None
    if isinstance(obj, str):
        return obj
    if isinstance(obj, CNAME_INDEX):
        try:
            return obj.value.name.value
        except Exception:
            return None
    if isinstance(obj, NAME):
        try:
            return obj.name.value
        except Exception:
            return None
    if hasattr(obj, "String"):
        s = obj.String
        if isinstance(s, str):
            return s
        if hasattr(s, "String"):
            return s.String
    if hasattr(obj, "name") and hasattr(obj.name, "value"):
        return obj.name.value
    if hasattr(obj, "value") and isinstance(obj.value, str):
        return obj.value
    return None


def _build_names_table(names, string_table):
    out = io.BytesIO()
    for name in names:
        offset = string_table.offset(name)
        hash_val = _fnv1a32(name.encode("iso-8859-1") + b"\x00")
        out.write(struct.pack("<II", offset, hash_val))
    return out.getvalue()


def _build_imports_table(imports, name_to_index, string_table):
    if not imports:
        return b""
    out = io.BytesIO()
    for imp in imports:
        out.write(
            struct.pack(
                "<IHH",
                string_table.offset(imp.path),
                name_to_index.get(imp.class_name, 0),
                imp.flags,
            )
        )
    return out.getvalue()


def _build_props_table(props):
    if not props:
        return b""
    out = io.BytesIO()
    for prop in props:
        out.write(
            struct.pack(
                "<HHHHQ",
                getattr(prop, "className", 0),
                getattr(prop, "classFlags", 0),
                getattr(prop, "propertyName", 0),
                getattr(prop, "propertyFlags", 0),
                getattr(prop, "hash", 0),
            )
        )
    return out.getvalue()


def _build_exports_table(exports, data_offset_base):
    out = io.BytesIO()
    for exp in exports:
        out.write(
            struct.pack(
                "<HHIIII",
                exp.class_name,
                exp.object_flags,
                exp.parent_id,
                exp.data_size,
                exp.data_offset + data_offset_base,
                exp.template,
            )
        )
        out.write(struct.pack("<I", exp.crc32))
    return out.getvalue()


def _build_buffers_table(buffers, data_offset_base):
    if not buffers:
        return b""
    out = io.BytesIO()
    for buf in buffers:
        out.write(
            struct.pack(
                "<IIIIII",
                buf.flags,
                buf.index,
                buf.offset + data_offset_base,
                buf.disk_size,
                buf.mem_size,
                buf.crc32,
            )
        )
    return out.getvalue()


def _calc_header_crc(version, flags, timestamp, build_version, file_size, buffer_size, num_chunks, table_headers):
    data = io.BytesIO()
    data.write(struct.pack("<I", MAGIC))
    data.write(struct.pack("<I", version))
    data.write(struct.pack("<I", flags))
    data.write(struct.pack("<Q", timestamp))
    data.write(struct.pack("<I", build_version))
    data.write(struct.pack("<I", file_size))
    data.write(struct.pack("<I", buffer_size))
    data.write(struct.pack("<I", DEADBEEF))
    data.write(struct.pack("<I", num_chunks))
    for th in table_headers:
        data.write(struct.pack("<III", th.offset, th.count, th.crc32))
    return _crc32(data.getvalue())


def _encode_chunk(chunk, name_to_index, import_index):
    out = io.BytesIO()
    out.write(_encode_cvariable(getattr(chunk, "PROPS", []) or [], name_to_index, import_index))

    if hasattr(chunk, "CMesh"):
        out.write(_encode_cmesh_buffers(chunk.CMesh, name_to_index))

    if hasattr(chunk, "CMaterialInstance"):
        params = getattr(chunk.CMaterialInstance, "InstanceParameters", None)
        out.write(_encode_instance_parameters(params, name_to_index, import_index))

    if hasattr(chunk, "CBitmapTexture"):
        out.write(_encode_cbitmap_texture(chunk.CBitmapTexture))

    # Post-property trailing data (e.g. events on CSkeletalAnimationSetEntry)
    post = getattr(chunk, "postPropsData", None)
    if post:
        out.write(post if isinstance(post, (bytes, bytearray)) else bytes(post))

    embedded = getattr(chunk, "embeddedAnimData", None)
    if embedded:
        out.write(embedded if isinstance(embedded, (bytes, bytearray)) else bytes(embedded))

    return out.getvalue()


def _encode_cvariable(props, name_to_index, import_index):
    out = io.BytesIO()
    out.write(struct.pack("<b", 0))
    for prop in props:
        out.write(_encode_property(prop, name_to_index, import_index))
    out.write(struct.pack("<H", 0))
    return out.getvalue()


def _encode_property(prop, name_to_index, import_index):
    name_id = name_to_index.get(prop.theName, 0)
    type_id = name_to_index.get(prop.theType, 0)
    value_bytes = _encode_property_value(prop, name_to_index, import_index)
    size = 4 + len(value_bytes)
    return struct.pack("<HHI", name_id, type_id, size) + value_bytes


def _encode_property_value(prop, name_to_index, import_index):
    p_type = getattr(prop, "theType", None)
    if p_type is None:
        return b""

    if p_type.endswith("StringAnsi"):
        value = getattr(prop, "String", None)
        if hasattr(value, "String"):
            value = value.String
        return _encode_string_ansi(value or "")

    if p_type == "String":
        value = getattr(prop, "String", None)
        if hasattr(value, "String"):
            value = value.String
        return _encode_cstring(value or "")

    if p_type == "CName":
        cname = _get_cname_value(prop)
        return struct.pack("<H", name_to_index.get(cname, 0))

    if p_type == "TagList":
        tags = list(getattr(prop, "TagList", None) or [])
        out = io.BytesIO()
        out.write(_write_vlq_count(len(tags)))
        for tag in tags:
            cname = _get_cname_value(tag)
            out.write(struct.pack("<H", name_to_index.get(cname, 0)))
        return out.getvalue()

    if p_type in Enums.Enum_Types:
        enum_obj = getattr(prop, "Index", None)
        if enum_obj is not None:
            if hasattr(enum_obj, "String") and enum_obj.String:
                return struct.pack("<H", name_to_index.get(enum_obj.String, 0))
            if hasattr(enum_obj, "strings") and enum_obj.strings:
                return struct.pack("<H", name_to_index.get(enum_obj.strings[0], 0))
        return struct.pack("<H", 0)

    if p_type in Enums.Enum_Flags_Types:
        enum_obj = getattr(prop, "Index", None)
        strings = []
        if enum_obj is not None:
            strings = getattr(enum_obj, "strings", [])
        if hasattr(prop, "strings"):
            strings = prop.strings
        out = io.BytesIO()
        for s in strings or []:
            out.write(struct.pack("<H", name_to_index.get(s, 0)))
        out.write(struct.pack("<H", 0))
        return out.getvalue()

    if p_type.startswith("handle:"):
        return _encode_handles(prop, import_index, allow_import=True)

    if p_type.startswith("soft:"):
        return _encode_soft(prop, import_index)

    if p_type.startswith("ptr:"):
        return _encode_handles(prop, import_index, allow_import=False)

    if p_type.startswith("array:"):
        return _encode_array(prop, name_to_index, import_index)

    if p_type == "DeferredDataBuffer":
        return struct.pack("<H", int(getattr(prop, "ValueA", 0)))

    if p_type == "CDateTime":
        dt = getattr(prop, "DateTime", None)
        value = getattr(dt, "Value", 0) if dt else 0
        return struct.pack("<Q", value)

    if p_type == "Float":
        return struct.pack("<f", float(getattr(prop, "Value", 0.0)))

    if p_type == "Uint32":
        return struct.pack("<I", int(getattr(prop, "Value", 0)))

    if p_type == "Uint16":
        return struct.pack("<H", int(getattr(prop, "Value", 0)))

    if p_type == "Uint8":
        return struct.pack("<B", int(getattr(prop, "Value", 0)))

    if p_type == "Int32":
        return struct.pack("<i", int(getattr(prop, "Value", 0)))

    if p_type == "Int16":
        return struct.pack("<h", int(getattr(prop, "Value", 0)))

    if p_type == "Int8":
        return struct.pack("<b", int(getattr(prop, "Value", 0)))

    if p_type == "Bool":
        return struct.pack("<B", 1 if getattr(prop, "Value", 0) else 0)

    subprops = _get_subprops(prop)
    if subprops:
        return _encode_cvariable(subprops, name_to_index, import_index)

    # fallback for unknown primitives
    if hasattr(prop, "Value"):
        try:
            return struct.pack("<I", int(prop.Value))
        except Exception:
            pass
    return b""


def _encode_array(prop, name_to_index, import_index):
    elements = getattr(prop, "elements", None)
    if elements is None:
        elements = []
    out = io.BytesIO()
    out.write(struct.pack("<I", len(elements)))

    elem_type = prop.theType.split(",")[-1]

    for el in elements:
        out.write(_encode_array_element(el, elem_type, name_to_index, import_index))

    return out.getvalue()


def _encode_array_element(el, elem_type, name_to_index, import_index):
    if elem_type.startswith("handle:"):
        return _encode_handle_value(el, import_index, allow_import=True)
    if elem_type.startswith("soft:"):
        return _encode_soft_value(el, import_index)
    if elem_type.startswith("ptr:"):
        return _encode_handle_value(el, import_index, allow_import=False)

    if elem_type == "String":
        if hasattr(el, "String"):
            el = el.String
        if hasattr(el, "String"):
            el = el.String
        return _encode_cstring(el or "")

    if elem_type == "CName":
        cname = _get_cname_value(el)
        return struct.pack("<H", name_to_index.get(cname, 0))

    if elem_type == "Float":
        return struct.pack("<f", float(getattr(el, "Value", el)))
    if elem_type == "Uint32":
        return struct.pack("<I", int(getattr(el, "Value", el)))
    if elem_type == "Uint16":
        return struct.pack("<H", int(getattr(el, "Value", el)))
    if elem_type == "Uint8":
        return struct.pack("<B", int(getattr(el, "Value", el)))
    if elem_type == "Int8":
        return struct.pack("<b", int(getattr(el, "Value", el)))
    if elem_type == "Bool":
        return struct.pack("<B", 1 if getattr(el, "Value", el) else 0)

    if isinstance(el, PROPERTY):
        return _encode_property_value(el, name_to_index, import_index)
    if isinstance(el, CVariantSizeNameType) and el.PROP:
        return _encode_variant_size_name_type(el, name_to_index, import_index)
    if isinstance(el, CMatrix4x4):
        return _encode_matrix4x4(el)

    # fallback
    return b""


def _encode_handles(prop, import_index, allow_import=True):
    handles = _get_handles(prop)
    out = io.BytesIO()
    if prop.theType.startswith("array:"):
        out.write(struct.pack("<I", len(handles)))
    if not handles:
        return out.getvalue()
    for h in handles:
        out.write(_encode_handle_value(h, import_index, allow_import=allow_import))
    return out.getvalue()


def _encode_soft(prop, import_index):
    handles = _get_handles(prop)
    if handles:
        return _encode_soft_value(handles[0], import_index)

    index_obj = getattr(prop, "Index", None)
    depot_path = getattr(index_obj, "Path", None)
    if depot_path:
        handle_like = type("_SoftHandle", (), {
            "ClassName": getattr(prop, "ClassName", None) or prop.theType.split(":", 1)[-1],
            "DepotPath": depot_path,
            "Flags": int(getattr(prop, "Flags", 4) or 4),
        })()
        return _encode_soft_value(handle_like, import_index)

    return struct.pack("<H", 0)


def _encode_handle_value(handle, import_index, allow_import=True):
    if getattr(handle, "ChunkHandle", False):
        ref = getattr(handle, "Reference", None)
        if ref is None:
            return struct.pack("<i", 0)
        return struct.pack("<i", int(ref) + 1)

    if not allow_import:
        return struct.pack("<i", 0)

    key = (getattr(handle, "ClassName", None), getattr(handle, "DepotPath", None), int(getattr(handle, "Flags", 0) or 0))
    idx = import_index.get(key, None)
    if idx is None:
        return struct.pack("<i", 0)
    return struct.pack("<i", -(idx + 1))


def _encode_soft_value(handle, import_index):
    key = (
        getattr(handle, "ClassName", None),
        getattr(handle, "DepotPath", None),
        int(getattr(handle, "Flags", 4) or 4),
    )
    idx = import_index.get(key, None)
    if idx is None:
        return struct.pack("<H", 0)
    return struct.pack("<H", int(idx) + 1)


def _encode_cmesh_buffers(cmesh, name_to_index):
    out = io.BytesIO()
    out.write(_encode_cbuffer_vlq_int32(getattr(cmesh, "ChunkgroupIndeces", None), name_to_index))
    out.write(_encode_cbuffer_vlq_int32(getattr(cmesh, "BoneNames", None), name_to_index))
    out.write(_encode_cbuffer_vlq_int32(getattr(cmesh, "Bonematrices", None), name_to_index))
    out.write(_encode_cbuffer_vlq_int32(getattr(cmesh, "Block3", None), name_to_index))
    out.write(_encode_cbuffer_vlq_int32(getattr(cmesh, "BoneIndecesMappingBoneIndex", None), name_to_index))
    return out.getvalue()


def _encode_cbitmap_texture(cbt):
    """Encode CBitmapTexture post-property binary data.

    Layout matches the reader at Types/VariousTypes.py (SMipData, CByteArray),
    with optional trailing unk1/unk2 fields for newer writer variants.
    """
    out = io.BytesIO()
    out.write(struct.pack("<I", int(getattr(getattr(cbt, "unk", None), "val", 0) or 0)))
    out.write(struct.pack("<I", int(getattr(getattr(cbt, "MipsCount", None), "val", 0) or 0)))
    for mip in getattr(getattr(cbt, "Mipdata", None), "bufferData", []) or []:
        mip_bytes = getattr(getattr(mip, "Mip", None), "Bytes", None) or b""
        out.write(struct.pack("<I", int(getattr(getattr(mip, "Width", None), "val", 0) or 0)))
        out.write(struct.pack("<I", int(getattr(getattr(mip, "Height", None), "val", 0) or 0)))
        out.write(struct.pack("<I", int(getattr(getattr(mip, "Blocksize", None), "val", 0) or 0)))
        out.write(struct.pack("<I", len(mip_bytes)))
        out.write(mip_bytes)
    out.write(struct.pack("<I", int(getattr(getattr(cbt, "ResidentmipSize", None), "val", 0) or 0)))

    unk1 = getattr(getattr(cbt, "unk1", None), "val", None)
    unk2 = getattr(getattr(cbt, "unk2", None), "val", None)
    if unk1 is not None or unk2 is not None:
        out.write(struct.pack("<H", int(unk1 or 0)))
        out.write(struct.pack("<H", int(unk2 or 0)))

    resident = getattr(getattr(cbt, "Residentmip", None), "val", None)
    if resident is not None:
        out.write(resident)
    return out.getvalue()


def _encode_cbuffer_vlq_int32(buffer_obj, name_to_index):
    if buffer_obj is None:
        return _write_vlq_count(0)

    elements = getattr(buffer_obj, "elements", None) or []
    out = io.BytesIO()
    out.write(_write_vlq_count(len(elements)))

    buffer_type = getattr(buffer_obj, "buffer_type", None)
    inner_type = getattr(buffer_obj, "inner_type", None)

    if buffer_type == CPaddedBuffer:
        for el in elements:
            out.write(_encode_cpadded_buffer(el, inner_type, name_to_index))
        return out.getvalue()

    for el in elements:
        out.write(_encode_buffer_element(el, buffer_type, name_to_index))

    return out.getvalue()


def _encode_cpadded_buffer(padded, inner_type, name_to_index):
    out = io.BytesIO()
    elements = getattr(padded, "elements", None) or []
    out.write(_write_bit6(len(elements)))
    for el in elements:
        out.write(_encode_buffer_element(el, inner_type, name_to_index))
    out.write(struct.pack("<f", float(getattr(padded, "padding", 0.0))))
    return out.getvalue()


def _encode_buffer_element(el, elem_type, name_to_index):
    if elem_type is None:
        # try best effort
        if hasattr(el, "val"):
            return struct.pack("<I", int(el.val))
        return b""

    if elem_type == CNAME_INDEX:
        cname = _get_cname_value(el)
        return struct.pack("<H", name_to_index.get(cname, 0))

    if elem_type == CMatrix4x4:
        return _encode_matrix4x4(el)

    if hasattr(el, "val"):
        val = el.val
        if elem_type.__name__ == "CFloat":
            return struct.pack("<f", float(val))
        if elem_type.__name__ == "CUInt16":
            return struct.pack("<H", int(val))
        if elem_type.__name__ == "CUInt32":
            return struct.pack("<I", int(val))
        if elem_type.__name__ == "CUInt8":
            return struct.pack("<B", int(val))

    if isinstance(el, (int, float)):
        return struct.pack("<I", int(el))

    return b""


def _encode_instance_parameters(params, name_to_index, import_index):
    out = io.BytesIO()
    elements = getattr(params, "elements", []) if params else []
    out.write(struct.pack("<I", len(elements)))
    for el in elements:
        if isinstance(el, CVariantSizeNameType) and el.PROP:
            out.write(_encode_variant_size_name_type(el, name_to_index, import_index))
    return out.getvalue()


def _encode_variant_size_name_type(variant, name_to_index, import_index):
    prop = variant.PROP
    value_bytes = _encode_property_value(prop, name_to_index, import_index)
    var_size = 8 + len(value_bytes)
    name_id = name_to_index.get(prop.theName, 0)
    type_id = name_to_index.get(prop.theType, 0)
    return struct.pack("<IHH", var_size, name_id, type_id) + value_bytes


def _encode_matrix4x4(mat):
    values = []
    if hasattr(mat, "fields") and mat.fields:
        values = mat.fields
    else:
        values = [
            mat.ax, mat.ay, mat.az, mat.aw,
            mat.bx, mat.by, mat.bz, mat.bw,
            mat.cx, mat.cy, mat.cz, mat.cw,
            mat.dx, mat.dy, mat.dz, mat.dw,
        ]
    out = io.BytesIO()
    for v in values:
        out.write(struct.pack("<f", float(v)))
    return out.getvalue()


def _encode_string_ansi(value):
    if not value:
        return b"\x00"
    requires_wide = any(ord(c) > 255 for c in value)
    if requires_wide:
        encoded = value.encode("utf-16le")
        num_wchars = len(value)
        if num_wchars > 127:
            num_wchars = 127  # clamp; very long strings unsupported
        return struct.pack("<B", 0x80 | num_wchars) + encoded
    else:
        encoded = value.encode("iso-8859-1")
        length = len(encoded)
        if length > 127:
            length = 127
            encoded = encoded[:127]
        return struct.pack("<B", length) + encoded


def _encode_cstring(value):
    if value is None:
        value = ""
    if value == "":
        return b"\x80"
    requires_wide = any(ord(c) > 255 for c in value)
    length = len(value)
    div, mod = divmod(length, 0x40)
    length -= (div * 0x40)
    b = length & 0x3F
    if not requires_wide:
        b |= 0x80
    if div != 0:
        b |= 0x40
    out = io.BytesIO()
    out.write(struct.pack("<B", b))
    if div != 0:
        out.write(struct.pack("<B", div))
    if requires_wide:
        out.write(value.encode("utf-16le"))
    else:
        out.write(value.encode("iso-8859-1"))
    return out.getvalue()


def _write_bit6(value):
    if value == 0:
        return b"\x80"
    bytes_out = []
    left = value
    i = 0
    while left > 0:
        if i == 0:
            bytes_out.append(left & 0x3F)
            left >>= 6
        else:
            bytes_out.append(left & 0xFF)
            left >>= 7
        i += 1

    for i in range(len(bytes_out)):
        last = i == len(bytes_out) - 1
        cleft = (len(bytes_out) - 1) - i
        if not last:
            if cleft >= 1 and i >= 1:
                bytes_out[i] |= 0x80
            elif bytes_out[i] < 64:
                bytes_out[i] |= 0x40
            else:
                bytes_out[i] |= 0x80
        if bytes_out[i] == 128:
            raise ValueError("Invalid Bit6 encoding")

    return bytes(bytes_out)


def _write_vlq_count(count):
    if count == 0:
        return b"\x80"
    return _write_vlq_int32(count)


def _write_vlq_int32(value):
    negative = value < 0
    value = abs(int(value))
    b = value & 0x3F
    value >>= 6
    if negative:
        b |= 0x80
    cont = value != 0
    if cont:
        b |= 0x40
    out = io.BytesIO()
    out.write(struct.pack("<B", b))
    while cont:
        b = value & 0x7F
        value >>= 7
        cont = value != 0
        if cont:
            b |= 0x80
        out.write(struct.pack("<B", b))
    return out.getvalue()


def _fnv1a32(data):
    h = 0x811C9DC5
    for b in data:
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


def _crc32(data):
    return zlib.crc32(data) & 0xFFFFFFFF
