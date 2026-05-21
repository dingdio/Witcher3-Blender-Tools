from ..CR2W.prop_utils import prop_to_string
import bpy
from bpy.props import StringProperty


_IMPORTED_FIELD_LIST_LIMIT = 6


def _format_float_list(values, precision=2):
    parts = []
    for value in values:
        try:
            parts.append(f"{float(value):.{precision}f}")
        except Exception:
            parts.append(str(value))
    return ",".join(parts)


def _format_engine_qs_transform(value):
    if not all(hasattr(value, attr) for attr in ("pitch", "yaw", "roll", "w")):
        return ""

    pos_values = [
        getattr(value, "x", 0.0) or 0.0,
        getattr(value, "y", 0.0) or 0.0,
        getattr(value, "z", 0.0) or 0.0,
    ]
    rot_values = [
        getattr(value, "pitch", 0.0) or 0.0,
        getattr(value, "yaw", 0.0) or 0.0,
        getattr(value, "roll", 0.0) or 0.0,
        getattr(value, "w", 1.0) or 0.0,
    ]
    scale_values = [
        getattr(value, "scale_x", 0.0) or 0.0,
        getattr(value, "scale_y", 0.0) or 0.0,
        getattr(value, "scale_z", 0.0) or 0.0,
    ]
    if not any(scale_values):
        scale_values = [1.0, 1.0, 1.0]

    parts = []
    if any(abs(float(item or 0.0)) > 0.00001 for item in pos_values):
        parts.append(f"Pos [{_format_float_list(pos_values)}]")
    parts.append(f"Rot [{_format_float_list(rot_values)}]")
    parts.append(f"Scale [{_format_float_list(scale_values)}]")
    return "  ".join(parts)


def _format_localized_string(value):
    if value is None or not hasattr(value, "val") or not hasattr(value, "text"):
        return ""

    line_id = str(getattr(value, "val", "") or "").strip()
    try:
        text = str(getattr(value, "text", "") or "").strip()
    except Exception:
        text = ""
    if line_id and text == line_id:
        text = ""

    if text and line_id:
        return f"{text} ({line_id})"
    if text:
        return text
    if line_id:
        return f"Unresolved LocalizedString: {line_id}"
    return ""


def _looks_like_object_repr(text):
    return " object at 0x" in str(text or "")


def _safe_text(value):
    text = str(value or "").strip()
    return "" if _looks_like_object_repr(text) else text


class WITCH_OT_ImportedFieldInfo(bpy.types.Operator):
    bl_idname = "witcher.imported_field_info"
    bl_label = "Field Info"
    bl_description = "Show the imported field type and current display value"

    field_name: StringProperty(default="")
    type_text: StringProperty(default="")
    value_text: StringProperty(default="")

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=520)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        layout.prop(self, "field_name", text="Field")
        layout.prop(self, "type_text", text="Type")
        layout.prop(self, "value_text", text="Value")

    def execute(self, context):
        return {'FINISHED'}


def _format_property_wrapper(value, depth=0):
    the_type = _safe_text(getattr(value, "theType", ""))
    if not the_type:
        return ""

    string_obj = getattr(value, "String", None)
    localized_string = _format_localized_string(string_obj)
    if localized_string:
        return localized_string

    if string_obj is not None:
        string_text = _safe_text(getattr(string_obj, "String", None))
        if not string_text and hasattr(string_obj, "ToString"):
            try:
                string_text = _safe_text(string_obj.ToString())
            except Exception:
                string_text = ""
        if string_text:
            return string_text

    guid_obj = getattr(value, "GUID", None)
    guid_text = _safe_text(getattr(guid_obj, "GuidString", ""))
    if guid_text:
        return guid_text

    index_obj = getattr(value, "Index", None)
    if index_obj is not None:
        index_text = _safe_text(getattr(index_obj, "String", ""))
        if not index_text and hasattr(index_obj, "ToString"):
            try:
                index_text = _safe_text(index_obj.ToString())
            except Exception:
                index_text = ""
        if index_text:
            return index_text

    if hasattr(value, "Value"):
        return _format_imported_field_value(getattr(value, "Value"), depth + 1)
    if hasattr(value, "ValueA"):
        return _format_imported_field_value(getattr(value, "ValueA"), depth + 1)

    for attr_name in ("MoreProps", "More", "PROPS", "value", "elements", "Handles"):
        if getattr(value, attr_name, None):
            return ""

    if "array" in the_type or "static:" in the_type:
        return "[]"
    return "\"\""


def _get_imported_field_type(value):
    if value is None:
        return ""

    the_type = _safe_text(getattr(value, "theType", ""))
    if the_type:
        return the_type

    if isinstance(value, bool):
        return "Bool"
    if isinstance(value, int):
        return "Int"
    if isinstance(value, float):
        return "Float"
    if isinstance(value, str):
        return "String"
    if isinstance(value, (list, tuple, set)):
        return f"array ({len(value)})"
    if isinstance(value, dict):
        return f"map ({len(value)})"

    class_name = _safe_text(value.__class__.__name__)
    return class_name


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
            text = _safe_text(value.get(key))
            if text:
                return text
        return ""

    animation = getattr(value, "animation", None)
    if animation is not None:
        text = _safe_text(getattr(animation, "name", ""))
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
        text = _safe_text(getattr(value, attr_name, ""))
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

    property_value = _format_property_wrapper(value, depth)
    if property_value:
        return property_value

    localized_string = _format_localized_string(value)
    if localized_string:
        return localized_string

    engine_qs_transform = _format_engine_qs_transform(value)
    if engine_qs_transform:
        return engine_qs_transform

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

    for attr_name in ("MoreProps", "More", "PROPS"):
        items = getattr(value, attr_name, None)
        if items:
            type_name = (
                str(getattr(value, "theType", "") or "").strip()
                or value.__class__.__name__
            )
            return f"{type_name} ({len(items)} fields)"

    iter_values = _iter_imported_values(value)
    if iter_values:
        return _format_imported_field_value(iter_values, depth + 1)

    try:
        prop_text = prop_to_string(value)
    except Exception:
        prop_text = ""
    if prop_text and not _looks_like_object_repr(prop_text):
        return prop_text

    label = _get_imported_value_label(value)
    if label:
        return label

    for attr_name in ("String", "Value", "val", "Path", "DepotPath", "name", "theName", "elementName"):
        attr = getattr(value, attr_name, None)
        if attr is None:
            continue
        if hasattr(attr, "String"):
            attr = getattr(attr, "String", "")
        text = _safe_text(attr)
        if text:
            return text

    text = _safe_text(value)
    return text if text else "\"\""




def _draw_imported_class_sections(layout, field_items, schema, show_unset, empty_label, per_class_show_unset=False):
    visible_any = False
    for class_name, _fields in schema:
        all_class_items = [
            item for item in field_items
            if str(getattr(item, "class_name", "") or "") == class_name
        ]
        if not all_class_items:
            continue

        toggle_item = all_class_items[0]
        class_show_unset = bool(show_unset)
        if per_class_show_unset and hasattr(toggle_item, "show_unset"):
            class_show_unset = class_show_unset or bool(getattr(toggle_item, "show_unset", False))

        class_items = [
            item for item in all_class_items
            if class_show_unset or bool(getattr(item, "is_set", False))
        ]
        if not class_items and not per_class_show_unset:
            continue

        visible_any = True
        class_box = layout.box()
        header = class_box.row(align=True)
        header.label(text=class_name, icon='PROPERTIES')
        if per_class_show_unset and hasattr(toggle_item, "show_unset"):
            header.prop(toggle_item, "show_unset", text="Unset", toggle=True)
        if not class_items:
            class_box.label(text="No set fields.", icon='INFO')
            continue

        col = class_box.column(align=True)
        col.use_property_split = True
        col.use_property_decorate = False
        for item in class_items:
            has_children = bool(getattr(item, "has_children", False))
            type_text = str(getattr(item, "type_text", "") or "").strip()
            if has_children:
                row = col.row(align=True)
                icon = 'TRIA_DOWN' if bool(getattr(item, "show_children", False)) else 'TRIA_RIGHT'
                row.prop(item, "show_children", text="", icon=icon, icon_only=True, emboss=False)
                row.prop(item, "value_text", text=item.field_name)
                info = row.operator(WITCH_OT_ImportedFieldInfo.bl_idname, text="", icon='INFO')
                info.field_name = str(getattr(item, "field_name", "") or "")
                info.type_text = type_text
                info.value_text = str(getattr(item, "value_text", "") or "")
                if bool(getattr(item, "show_children", False)):
                    _draw_imported_field_children(col, item)
            else:
                row = col.row(align=True)
                row.label(text="", icon='BLANK1')
                row.prop(item, "value_text", text=item.field_name)
                info = row.operator(WITCH_OT_ImportedFieldInfo.bl_idname, text="", icon='INFO')
                info.field_name = str(getattr(item, "field_name", "") or "")
                info.type_text = type_text
                info.value_text = str(getattr(item, "value_text", "") or "")

    if not visible_any:
        layout.label(text=empty_label, icon='INFO')


def _draw_imported_field_children(layout, item, parent_key=""):
    children = [
        child for child in getattr(item, "children", []) or []
        if str(getattr(child, "parent_key", "") or "") == parent_key
    ]
    if not children:
        return

    box = layout.box()
    box.use_property_split = True
    box.use_property_decorate = False
    for child in children:
        depth = max(0, int(getattr(child, "depth", 0) or 0))
        label = f"{'  ' * depth}{getattr(child, 'label', '') or 'item'}"
        has_children = bool(getattr(child, "has_children", False))
        type_text = str(getattr(child, "type_text", "") or "").strip()
        if has_children:
            row = box.row(align=True)
            icon = 'TRIA_DOWN' if bool(getattr(child, "show_children", False)) else 'TRIA_RIGHT'
            row.prop(child, "show_children", text="", icon=icon, icon_only=True, emboss=False)
            row.prop(child, "value_text", text=label)
            info = row.operator(WITCH_OT_ImportedFieldInfo.bl_idname, text="", icon='INFO')
            info.field_name = str(getattr(child, "label", "") or "")
            info.type_text = type_text
            info.value_text = str(getattr(child, "value_text", "") or "")
            if bool(getattr(child, "show_children", False)):
                _draw_imported_field_children(box, item, str(getattr(child, "item_key", "") or ""))
        else:
            row = box.row(align=True)
            row.label(text="", icon='BLANK1')
            row.prop(child, "value_text", text=label)
            info = row.operator(WITCH_OT_ImportedFieldInfo.bl_idname, text="", icon='INFO')
            info.field_name = str(getattr(child, "label", "") or "")
            info.type_text = type_text
            info.value_text = str(getattr(child, "value_text", "") or "")
