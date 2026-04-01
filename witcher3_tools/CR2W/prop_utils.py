"""Shared helpers for reading parsed CR2W PROPERTY values."""


def read_cname_prop(prop):
    """Extract string from a CName PROPERTY."""
    if prop is None:
        return ""
    idx = getattr(prop, "Index", None)
    if idx is None:
        return ""
    return str(getattr(idx, "String", None) or "")


def read_float_prop(prop):
    """Extract float from a Float PROPERTY."""
    if prop is None:
        return 0.0
    return float(getattr(prop, "Value", 0.0) or 0.0)


def read_string_prop(prop):
    """Extract string from a String/StringAnsi PROPERTY."""
    if prop is None:
        return ""
    string_obj = getattr(prop, "String", None)
    if string_obj is not None:
        return str(getattr(string_obj, "String", None) or string_obj or "")
    value = getattr(prop, "Value", None)
    if value is not None:
        return str(value)
    return ""


def prop_to_string(prop, default=""):
    """Extract the best-effort scalar string from a PROPERTY."""
    if prop is None:
        return default

    value = None
    try:
        value = prop.ToString()
    except Exception:
        value = None

    if hasattr(value, "value"):
        value = value.value

    if not isinstance(value, str):
        value = read_string_prop(prop) or read_cname_prop(prop)

    if not value:
        raw_value = getattr(prop, "Value", None)
        if raw_value is not None:
            value = str(raw_value)

    text = str(value or "").strip()
    return text or default


def read_bool_prop(prop):
    """Extract bool from a Bool PROPERTY."""
    if prop is None:
        return False
    value = getattr(prop, "Value", None)
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    try:
        return bool(int(value))
    except (TypeError, ValueError):
        return bool(value)


def read_int_prop(prop):
    """Extract integer from an integer PROPERTY."""
    if prop is None:
        return 0
    value = getattr(prop, "Value", None)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def read_datetime_prop(prop):
    """Extract display string from a CDateTime PROPERTY."""
    if prop is None:
        return ""
    dt = getattr(prop, "DateTime", None)
    if dt is None:
        return ""
    return str(getattr(dt, "String", None) or getattr(dt, "Value", None) or "")


def read_enum_prop(prop):
    """Extract a readable enum value from an enum PROPERTY."""
    if prop is None:
        return ""

    idx = getattr(prop, "Index", None)
    if idx is not None:
        strings = list(getattr(idx, "strings", None) or [])
        if strings:
            return " | ".join(str(item) for item in strings if str(item or "").strip())
        value = getattr(idx, "String", None)
        if value is not None:
            return str(value)
        if hasattr(idx, "ToString"):
            try:
                return str(idx.ToString() or "")
            except Exception:
                pass

    strings = list(getattr(prop, "strings", None) or [])
    if strings:
        return " | ".join(str(item) for item in strings if str(item or "").strip())

    return ""


def chunk_label(chunk):
    """Return the most readable label available for a referenced chunk."""
    if chunk is None:
        return ""

    name_prop = chunk.GetVariableByName("name") if hasattr(chunk, "GetVariableByName") else None
    value = read_string_prop(name_prop) or read_cname_prop(name_prop)
    if value:
        return value

    return str(getattr(chunk, "name", "") or getattr(chunk, "Type", "") or "").strip()


def read_array_string_prop(prop):
    """Extract a list of strings from an array PROPERTY."""
    if prop is None:
        return []

    elements = getattr(prop, "elements", None)
    if elements is None:
        chunks = getattr(prop, "chunks", None)
        elements = getattr(chunks, "elements", None) if chunks is not None else None
    if elements is None:
        elements = getattr(prop, "PROPS", None)
    if not elements:
        return []

    values = []
    for elem in elements:
        value = read_string_prop(elem) or read_cname_prop(elem)
        if not value:
            value = getattr(elem, "Value", None) or getattr(elem, "val", None) or ""
        value = str(value or "").strip()
        if value:
            values.append(value)

    return values


def read_taglist_prop(prop):
    """Extract a list of strings from a TagList PROPERTY."""
    if prop is None:
        return []

    values = []
    for tag in getattr(prop, "TagList", None) or []:
        value = getattr(tag, "value", None)
        if value is None:
            value = getattr(tag, "String", None)
        value = str(value or "").strip()
        if value:
            values.append(value)

    return values


def read_handle_paths_prop(prop):
    """Extract import depot paths from a handle-array PROPERTY."""
    if prop is None:
        return []

    values = []
    for handle in getattr(prop, "Handles", None) or []:
        value = getattr(handle, "DepotPath", None) or getattr(handle, "path", None) or ""
        value = str(value or "").strip()
        if value:
            values.append(value)

    return values


def read_handle_label(handle, chunks):
    """Extract a readable label from a HANDLE object."""
    if handle is None:
        return ""

    value = getattr(handle, "DepotPath", None) or getattr(handle, "path", None) or ""
    value = str(value or "").strip()
    if value:
        return value

    ref = getattr(handle, "Reference", None)
    if isinstance(ref, int) and 0 <= ref < len(chunks):
        return chunk_label(chunks[ref])

    raw_value = getattr(handle, "val", None)
    if raw_value is None:
        raw_value = getattr(handle, "Value", None)
    try:
        idx = int(raw_value)
    except (TypeError, ValueError):
        idx = None
    if idx is not None and 1 <= idx <= len(chunks):
        return chunk_label(chunks[idx - 1])

    return str(getattr(handle, "ClassName", None) or "").strip()


def read_handle_labels_prop(prop, chunks):
    """Extract readable labels from a handle PROPERTY or handle-array PROPERTY."""
    if prop is None:
        return []

    values = []
    for handle in getattr(prop, "Handles", None) or []:
        value = read_handle_label(handle, chunks)
        if value:
            values.append(value)

    return values


def read_single_handle_label_prop(prop, chunks):
    """Extract a single readable label from a handle PROPERTY."""
    values = read_handle_labels_prop(prop, chunks)
    return values[0] if values else ""


def read_ptr_chunk_labels_prop(prop, chunks):
    """Extract readable labels from a ptr-array PROPERTY."""
    if prop is None:
        return []

    values = []
    for ptr in getattr(prop, "value", None) or []:
        try:
            idx = int(ptr)
        except (TypeError, ValueError):
            continue
        if idx < 1 or idx > len(chunks):
            continue

        value = chunk_label(chunks[idx - 1])
        value = str(value or "").strip()
        if value:
            values.append(value)

    return values


def iter_prop_elements(prop):
    elements = getattr(prop, "elements", None)
    if elements is not None:
        return list(elements)

    chunks = getattr(prop, "chunks", None)
    elements = getattr(chunks, "elements", None) if chunks is not None else None
    if elements is not None:
        return list(elements)

    return []


def read_prop_struct_items(items, chunks):
    values = {}
    for item in items or []:
        field_name = str(getattr(item, "theName", "") or getattr(item, "elementName", "") or "").strip()
        if not field_name:
            continue
        values[field_name] = read_prop_value(item, chunks)
    return values


def read_prop_value(prop, chunks):
    """Convert a PROPERTY/ELEMENT object into plain Python display data."""
    if prop is None:
        return None

    if isinstance(prop, (str, int, float, bool)):
        return prop

    the_type = str(getattr(prop, "theType", "") or "")
    if the_type == "Bool":
        return read_bool_prop(prop)
    if the_type in ("Float", "CFloat"):
        return read_float_prop(prop)
    if the_type in ("Uint8", "Uint16", "Uint32", "Int8", "Int16", "Int32"):
        return read_int_prop(prop)
    if the_type == "CDateTime":
        return read_datetime_prop(prop)
    if the_type == "CName":
        return read_cname_prop(prop)
    if the_type in ("String", "StringAnsi"):
        return read_string_prop(prop)
    if the_type == "TagList":
        return read_taglist_prop(prop)
    if "handle:" in the_type:
        labels = read_handle_labels_prop(prop, chunks)
        if "array:" in the_type:
            return labels
        return labels[0] if labels else ""
    if "ptr:" in the_type and hasattr(prop, "value"):
        return read_ptr_chunk_labels_prop(prop, chunks)

    enum_value = read_enum_prop(prop)
    if enum_value:
        return enum_value

    elements = iter_prop_elements(prop)
    if elements:
        return [read_prop_value(item, chunks) for item in elements]

    for attr_name in ("More", "MoreProps", "PROPS"):
        items = getattr(prop, attr_name, None)
        if items:
            return read_prop_struct_items(items, chunks)

    text = read_string_prop(prop) or read_cname_prop(prop)
    if text:
        return text

    value = getattr(prop, "Value", None)
    if value is not None:
        return value

    if hasattr(prop, "ToString"):
        try:
            text = str(prop.ToString() or "")
            if text:
                return text
        except Exception:
            pass

    return None
