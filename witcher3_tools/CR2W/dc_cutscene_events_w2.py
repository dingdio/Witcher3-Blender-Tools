"""Witcher 2 cutscene event parsing.

W2 cutscenes store event arrays in CAnimEventSerializer chunks. The event
classes often share names with W3 classes, but their serialized fields are the
older W2 layouts, so this module keeps the binary handling separate.
"""

import logging
import struct

from .CR2W_types import CBufferUInt32, CVariantSizeType
from .cutscene_event_schema import GAME_W2, build_event_data
from .prop_utils import (
    read_bool_prop,
    read_cname_prop,
    read_enum_prop,
    read_float_prop,
    read_int_prop,
    read_prop_value,
    read_string_prop,
)


log = logging.getLogger(__name__)


_MAX_W2_SERIALIZED_EVENTS = 4096


def _iter_event_props(event_prop):
    for attr_name in ("More", "MoreProps", "PROPS"):
        items = getattr(event_prop, attr_name, None)
        if items:
            return list(items)
    return []


def _event_prop_by_name(event_prop, prop_name):
    prop_name = str(prop_name or "")
    for prop in _iter_event_props(event_prop):
        if str(getattr(prop, "theName", "") or "") == prop_name:
            return prop
    return None


def _event_type_name(event_prop):
    type_name = str(getattr(event_prop, "theType", "") or "")
    if type_name:
        return type_name
    type_info = getattr(event_prop, "Type", None)
    type_name = str(getattr(type_info, "type", "") or "")
    if type_name:
        return type_name
    return str(getattr(event_prop, "name", "") or "")


def _event_scalar_value(prop, chunks):
    if prop is None:
        return None
    type_name = str(getattr(prop, "theType", "") or "")
    if type_name == "CName":
        return read_cname_prop(prop)
    if type_name in {"String", "StringAnsi"}:
        return read_string_prop(prop)
    if type_name in {"Float", "CFloat"}:
        return read_float_prop(prop)
    if type_name == "Bool":
        return read_bool_prop(prop)
    if type_name in {"Uint8", "Uint16", "Uint32", "Int8", "Int16", "Int32"}:
        return read_int_prop(prop)
    enum_value = read_enum_prop(prop)
    if enum_value:
        return enum_value
    return read_prop_value(prop, chunks)


def _event_raw_fields(event_prop, chunks):
    fields = {}
    for prop in _iter_event_props(event_prop):
        field_name = str(getattr(prop, "theName", "") or "").strip()
        if not field_name:
            continue
        fields[field_name] = _event_scalar_value(prop, chunks)
    return fields


def _parse_w2_event_variant(event_prop, chunks):
    if event_prop is None:
        return None

    type_name = _event_type_name(event_prop)
    if not type_name:
        return None

    raw_fields = _event_raw_fields(event_prop, chunks)
    return build_event_data(GAME_W2, type_name, raw_fields)


def _serializer_chunk_index(cr2w_file, serializer_chunk):
    chunks = list(getattr(getattr(cr2w_file, "CHUNKS", None), "CHUNKS", None) or [])
    for idx, chunk in enumerate(chunks):
        if chunk is serializer_chunk:
            return idx

    raw_idx = getattr(serializer_chunk, "ChunkIndex", None)
    if isinstance(raw_idx, int) and 0 <= raw_idx < len(chunks):
        return raw_idx
    return None


def read_w2_event_serializer(cr2w_file, raw_data, serializer_chunk):
    if cr2w_file is None or serializer_chunk is None:
        return []
    if str(getattr(serializer_chunk, "name", "") or getattr(serializer_chunk, "Type", "") or "") != "CAnimEventSerializer":
        return []

    chunk_idx = _serializer_chunk_index(cr2w_file, serializer_chunk)
    if chunk_idx is None:
        return []

    exports = list(getattr(cr2w_file, "CR2WExport", None) or [])
    if chunk_idx < 0 or chunk_idx >= len(exports):
        return []

    export = exports[chunk_idx]
    start = int(getattr(cr2w_file, "start", 0) or 0)
    offset = int(getattr(export, "dataOffset", 0) or 0) + start
    size = int(getattr(export, "dataSize", 0) or 0)
    if size < 4 or offset < 0 or offset + 4 > len(raw_data):
        return []

    try:
        event_count = struct.unpack_from("<I", raw_data, offset)[0]
    except Exception:
        return []
    if event_count <= 0:
        return []
    if event_count > _MAX_W2_SERIALIZED_EVENTS:
        log.warning(
            "Skipping W2 CAnimEventSerializer with implausible event count %d in %s",
            event_count,
            getattr(cr2w_file, "fileName", ""),
        )
        return []

    file_name = str(getattr(cr2w_file, "fileName", "") or "")
    if not file_name:
        return []

    try:
        with open(file_name, "rb") as f:
            f.seek(offset)
            buffer = CBufferUInt32(cr2w_file, CVariantSizeType)
            buffer.Read(f, 0)
    except Exception:
        log.debug("Failed reading W2 CAnimEventSerializer chunk %s", chunk_idx + 1, exc_info=True)
        return []

    events = []
    chunks = list(getattr(getattr(cr2w_file, "CHUNKS", None), "CHUNKS", None) or [])
    for variant in getattr(buffer, "elements", None) or []:
        try:
            event = _parse_w2_event_variant(getattr(variant, "PROP", None), chunks)
        except Exception:
            log.debug("Failed parsing W2 cutscene event variant", exc_info=True)
            event = None
        if event is not None:
            events.append(event)

    if events:
        log.info("Parsed %d W2 cutscene events from CAnimEventSerializer #%d", len(events), chunk_idx + 1)
    return events


def _iter_handle_chunk_indices(prop):
    if prop is None:
        return

    for handle in getattr(prop, "Handles", None) or []:
        value = getattr(handle, "val", None)
        if value is None:
            value = getattr(handle, "Value", None)
        if value is None:
            ref_idx = getattr(handle, "Reference", None)
            if isinstance(ref_idx, int):
                value = ref_idx + 1
        try:
            idx = int(value)
        except (TypeError, ValueError):
            continue
        if idx > 0:
            yield idx

    value = getattr(prop, "Value", None)
    if value is None:
        value = getattr(prop, "value", None)
    if isinstance(value, (list, tuple)):
        values = value
    else:
        values = [value]
    for item in values:
        try:
            idx = int(item)
        except (TypeError, ValueError):
            continue
        if idx > 0:
            yield idx


def read_w2_event_serializer_prop(cr2w_file, raw_data, prop, chunks, seen=None):
    events = []
    if seen is None:
        seen = set()
    for chunk_idx in _iter_handle_chunk_indices(prop) or []:
        if chunk_idx in seen:
            continue
        seen.add(chunk_idx)
        if chunk_idx < 1 or chunk_idx > len(chunks):
            continue
        events.extend(read_w2_event_serializer(cr2w_file, raw_data, chunks[chunk_idx - 1]))
    return events


def read_w2_cutscene_root_events(cr2w_file, raw_data, cutscene_chunk, chunks):
    events = []
    seen = set()
    for prop_name in ("animEvents", "events", "extAnimEvents"):
        prop = cutscene_chunk.GetVariableByName(prop_name) if cutscene_chunk is not None else None
        events.extend(read_w2_event_serializer_prop(cr2w_file, raw_data, prop, chunks, seen=seen))
    return events


def read_w2_cutscene_entry_events(cr2w_file, raw_data, entry_chunk, chunks):
    prop = entry_chunk.GetVariableByName("events") if entry_chunk is not None else None
    return read_w2_event_serializer_prop(cr2w_file, raw_data, prop, chunks)
