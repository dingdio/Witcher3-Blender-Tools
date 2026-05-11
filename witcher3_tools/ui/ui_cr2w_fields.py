from ..CR2W.prop_utils import prop_to_string


_IMPORTED_FIELD_LIST_LIMIT = 6


def _iter_imported_values(prop):
    if prop is None:
        return []
    if isinstance(prop, (list, tuple, set)):
        return list(prop)
    for attr in ("value", "More", "elements", "Handles"):
        values = getattr(prop, attr, None)
        if values is not None:
            if isinstance(values, (list, tuple, set)):
                return list(values)
            return [values]
    return []


def _get_present_imported_fields(imported_data):
    return {
        str(field_name or "").strip()
        for field_name in (
            getattr(imported_data, "presentPropertyNames", None)
            or getattr(imported_data, "presentTemplateProps", None)
            or set()
        )
        if str(field_name or "").strip()
    }


def _get_imported_field_schema(imported_data, fallback_schema=()):
    schema = getattr(imported_data, "importedClassFieldSchema", None) if imported_data is not None else None
    return schema or fallback_schema


def _get_imported_field_value(imported_data, field_name):
    if imported_data is None:
        return None
    return getattr(imported_data, field_name, None)


def _get_imported_value_label(value):
    if value is None:
        return ""

    if isinstance(value, dict):
        for key in ("name", "Name", "$type"):
            text = str(value.get(key) or "").strip()
            if text:
                return text
        return ""

    animation = getattr(value, "animation", None)
    if animation is not None:
        text = str(getattr(animation, "name", "") or "").strip()
        if text:
            return text

    for attr_name in (
        "name",
        "sectionName",
        "elementID",
        "id",
        "actorName",
        "slotName",
        "cameraName",
        "eventName",
        "animationName",
        "template",
        "type_name",
    ):
        text = str(getattr(value, attr_name, "") or "").strip()
        if text:
            return text

    return ""


def _format_imported_field_value(value, depth=0):
    if depth > 4:
        label = _get_imported_value_label(value)
        return label or value.__class__.__name__

    if value is None:
        return "\"\""

    if isinstance(value, bool):
        return "True" if bool(value) else "False"

    if isinstance(value, (int, float)):
        return f"{float(value):g}" if isinstance(value, float) else str(value)

    if isinstance(value, str):
        return value if value else "\"\""

    if isinstance(value, dict):
        if not value:
            return "{}"
        parts = []
        items = list(value.items())
        for key, item_value in items[:_IMPORTED_FIELD_LIST_LIMIT]:
            parts.append(f"{key}={_format_imported_field_value(item_value, depth + 1)}")
        text = ", ".join(parts) if parts else "{}"
        if len(items) > _IMPORTED_FIELD_LIST_LIMIT:
            text += f" (+{len(items) - _IMPORTED_FIELD_LIST_LIMIT} more)"
        return text

    if isinstance(value, (list, tuple, set)):
        seq = list(value)
        if not seq:
            return "[]"
        items = []
        for item in seq[:_IMPORTED_FIELD_LIST_LIMIT]:
            label = _get_imported_value_label(item)
            items.append(label or _format_imported_field_value(item, depth + 1))
        text = ", ".join(item for item in items if item)
        if len(seq) > _IMPORTED_FIELD_LIST_LIMIT:
            text += f" (+{len(seq) - _IMPORTED_FIELD_LIST_LIMIT} more)"
        return text or "[]"

    guid_obj = getattr(value, "GUID", None)
    guid_text = str(getattr(guid_obj, "GuidString", "") or "").strip()
    if guid_text:
        return guid_text

    engine_transform = getattr(value, "EngineTransform", None)
    if engine_transform is not None and engine_transform is not value:
        return _format_imported_field_value(engine_transform, depth + 1)

    if all(hasattr(value, attr) for attr in ("X", "Y", "Z")):
        parts = [f"{attr}={float(getattr(value, attr, 0.0) or 0.0):g}" for attr in ("X", "Y", "Z")]
        for attr in ("Pitch", "Yaw", "Roll"):
            if hasattr(value, attr):
                parts.append(f"{attr}={float(getattr(value, attr, 0.0) or 0.0):g}")
        return ", ".join(parts)

    iter_values = _iter_imported_values(value)
    if iter_values:
        return _format_imported_field_value(iter_values, depth + 1)

    try:
        prop_text = prop_to_string(value)
    except Exception:
        prop_text = ""
    if prop_text:
        return prop_text

    label = _get_imported_value_label(value)
    if label:
        return label

    text = str(value or "").strip()
    return text if text else "\"\""




def _draw_imported_class_sections(layout, field_items, schema, show_unset, empty_label):
    visible_any = False
    for class_name, _fields in schema:
        class_items = [
            item for item in field_items
            if str(getattr(item, "class_name", "") or "") == class_name
            and (show_unset or bool(getattr(item, "is_set", False)))
        ]
        if not class_items:
            continue

        visible_any = True
        class_box = layout.box()
        class_box.label(text=class_name, icon='PROPERTIES')
        col = class_box.column(align=True)
        col.use_property_split = True
        col.enabled = False
        for item in class_items:
            col.prop(item, "value_text", text=item.field_name)

    if not visible_any:
        layout.label(text=empty_label, icon='INFO')
