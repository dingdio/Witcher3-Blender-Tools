import logging
from types import SimpleNamespace

from .bin_helpers import ReadVLQInt32, getStringOfLen, readInt32, readU32, readU64, readUShort

log = logging.getLogger(__name__)

UPDATED_RESOURCE_FORMAT_VERSION = 155
OLD_VERSION_HEADER_BUFFER_ENTRY_VERSION = 153
OLD_VERSION_16BIT_OBJECT_FLAGS_VERSION = 124
OLD_VERSION_SOFT_IMPORT_FLAG = 1 << 2

OLD_OBJECT_FLAG_INLINED = 1 << 4
OLD_OBJECT_FLAG_SCRIPTED = 1 << 7
OLD_OBJECT_FLAG_TRANSIENT = 1 << 9
OLD_OBJECT_FLAG_REFERENCED = 1 << 10
OLD_OBJECT_FLAG_DEFAULT_OBJECT = 1 << 12
OLD_OBJECT_FLAG_SCRIPT_CREATED = 1 << 13
OLD_OBJECT_FLAG_HAS_HANDLE = 1 << 14
OLD_OBJECT_FLAG_WAS_COOKED = 1 << 16

OBJECT_FLAG_INLINED = 1 << 3
OBJECT_FLAG_SCRIPTED = 1 << 4
OBJECT_FLAG_TRANSIENT = 1 << 6
OBJECT_FLAG_REFERENCED = 1 << 7
OBJECT_FLAG_DEFAULT_OBJECT = 1 << 9
OBJECT_FLAG_SCRIPT_CREATED = 1 << 10
OBJECT_FLAG_HAS_HANDLE = 1 << 11
OBJECT_FLAG_WAS_COOKED = 1 << 13


def is_old_version(version):
    return 115 < version < UPDATED_RESOURCE_FORMAT_VERSION


def _read_red_serialized_string(f):
    char_count = ReadVLQInt32(f)
    if char_count == 0:
        return ""
    if char_count < 0:
        return getStringOfLen(f, -char_count).rstrip("\x00")
    return f.read(char_count * 2).decode("utf-16le", errors="replace").rstrip("\x00")


def _read_old_version_resource_path(f):
    char_count = ReadVLQInt32(f)
    if char_count == 0:
        return ""
    if char_count < 0:
        return getStringOfLen(f, -char_count).rstrip("\x00")
    return getStringOfLen(f, char_count).rstrip("\x00")


def _normalize_depot_path(path):
    return path.replace("/", "\\").lower()


def _remap_old_version_object_flags(flags):
    remapped = 0
    if flags & OLD_OBJECT_FLAG_INLINED:
        remapped |= OBJECT_FLAG_INLINED
    if flags & OLD_OBJECT_FLAG_SCRIPTED:
        remapped |= OBJECT_FLAG_SCRIPTED
    if flags & OLD_OBJECT_FLAG_TRANSIENT:
        remapped |= OBJECT_FLAG_TRANSIENT
    if flags & OLD_OBJECT_FLAG_REFERENCED:
        remapped |= OBJECT_FLAG_REFERENCED
    if flags & OLD_OBJECT_FLAG_DEFAULT_OBJECT:
        remapped |= OBJECT_FLAG_DEFAULT_OBJECT
    if flags & OLD_OBJECT_FLAG_SCRIPT_CREATED:
        remapped |= OBJECT_FLAG_SCRIPT_CREATED
    if flags & OLD_OBJECT_FLAG_HAS_HANDLE:
        remapped |= OBJECT_FLAG_HAS_HANDLE
    if flags & OLD_OBJECT_FLAG_WAS_COOKED:
        remapped |= OBJECT_FLAG_WAS_COOKED
    return remapped


def _table(name, offset=0, count=0):
    return SimpleNamespace(tableName=name, offset=offset, itemCount=count)


def load_old_version_resource_tables(cr2w, f, start):
    from .CR2W_types import CR2WExport, CR2WImport
    from .Types.VariousTypes import NAME

    version = cr2w.HEADER.version

    names_offset = readU32(f)
    names_count = readU32(f)
    exports_offset = readU32(f)
    exports_count = readU32(f)
    imports_offset = readU32(f)
    imports_count = readU32(f)
    soft_offset = readU32(f)
    soft_count = readU32(f)
    if version >= OLD_VERSION_HEADER_BUFFER_ENTRY_VERSION:
        _buffer_offset = readU32(f)
        buffer_table_offset = readU32(f)
        buffer_count = readU32(f)
    else:
        buffer_table_offset = 0
        buffer_count = 0

    total_imports = imports_count + max(soft_count - 1, 0)
    cr2w.CR2WTable = [
        _table("Strings", names_offset, names_count),
        _table("Enums", names_offset, names_count),
        _table("CR2WImport", imports_offset, total_imports),
        _table("CR2WProperty", 0, 0),
        _table("CR2WExport", exports_offset, exports_count),
        _table("CR2WBuffer", buffer_table_offset, buffer_count),
    ]
    while len(cr2w.CR2WTable) < 10:
        cr2w.CR2WTable.append(_table("Unknown"))

    cr2w.maxExport = exports_count
    cr2w.STRINGS = []
    cr2w.CNAMES = [NAME(name="")]
    if names_offset > 0:
        f.seek(names_offset + start)
        for _ in range(names_count):
            name = _read_red_serialized_string(f)
            cr2w.STRINGS.append(name)
            cr2w.CNAMES.append(NAME(name=name))

    cr2w.CR2WImport = []
    if imports_offset > 0:
        f.seek(imports_offset + start)
        for _ in range(imports_count):
            cr2w.CR2WImport.append(
                CR2WImport(
                    path=_normalize_depot_path(_read_old_version_resource_path(f)),
                    className=readUShort(f),
                    flags=readUShort(f),
                )
            )

    if soft_offset > 0 and soft_count > 1:
        f.seek(soft_offset + start)
        for _ in range(1, soft_count):
            cr2w.CR2WImport.append(
                CR2WImport(
                    path=_normalize_depot_path(_read_red_serialized_string(f)),
                    className=0,
                    flags=OLD_VERSION_SOFT_IMPORT_FLAG,
                )
            )

    cr2w.CR2W_Property = []
    cr2w.CR2WExport = []
    if exports_offset > 0:
        f.seek(exports_offset + start)
        for _ in range(exports_count):
            class_name = readUShort(f)
            parent_id = readU32(f)
            data_size = readU32(f)
            data_offset = readU32(f)
            if version < OLD_VERSION_16BIT_OBJECT_FLAGS_VERSION:
                object_flags = _remap_old_version_object_flags(readU32(f))
            else:
                object_flags = readUShort(f)
            template = readInt32(f)
            _read_red_serialized_string(f)
            readU64(f)
            cr2w.CR2WExport.append(
                CR2WExport(
                    className=class_name,
                    objectFlags=object_flags,
                    parentID=parent_id,
                    dataSize=data_size,
                    dataOffset=data_offset,
                    template=template,
                    crc32=0,
                    name=cr2w.CNAMES[class_name].name.value if class_name < len(cr2w.CNAMES) else "",
                )
            )

    cr2w.CR2WBuffer = []
    cr2w.BufferData = []
    if buffer_count:
        log.warning("Old-version buffer tables are not implemented yet for %s (count=%s).", cr2w.fileName, buffer_count)
