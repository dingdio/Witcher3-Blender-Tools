import json

import bpy

from ..source_game_paths import normalize_source_game as _normalize_source_game


_EQUIPMENT_ITEM_PICKER_RECENTS = {}
_EQUIPMENT_ITEM_PICKER_RECENT_LIMIT = 24
_EQUIPMENT_ITEM_PICKER_WIDTH = 720
_EQUIPMENT_ITEM_PICKER_GRID_PAGE_ROWS = 4
_EQUIPMENT_ITEM_PICKER_GRID_SIZES = {
    'S': (6, 5.0),
    'M': (5, 6.5),
    'L': (4, 8.0),
}
_EQUIPMENT_ITEM_PICKER_GRID_DEFAULT_SIZE = 'L'
_EQUIPMENT_ITEM_PICKER_LIST_PAGE_ROWS = 8
_EQUIPMENT_ITEM_PICKER_LIST_ICON_SCALE = 3.0
_EQUIPMENT_ITEM_PICKER_LIST_LABEL_CHARS = 60
_EQUIPMENT_PRESET_PICKER_WIDTH = 860
_EQUIPMENT_PRESET_PICKER_LIST_PAGE_ROWS = 4
_EQUIPMENT_PRESET_PICKER_LIST_ICON_SCALE = 6.0
_EQUIPMENT_PRESET_PICKER_LIST_LABEL_CHARS = 52
_EQUIPMENT_PRESET_PICKER_PREVIEW_COUNT = 6
_EQUIPMENT_PRESET_PICKER_PREVIEW_CATEGORIES = (
    "pants",
    "armor",
    "gloves",
    "boots",
    "steelsword",
    "silversword",
)
_EQUIPMENT_PRESET_PICKER_GRID_PAGE_ROWS = 3
_EQUIPMENT_PRESET_PICKER_GRID_LABEL_CHARS = 28
_EQUIPMENT_PRESET_PICKER_CATEGORY_ORDER = {
    "pants": 0,
    "armor": 1,
    "gloves": 2,
    "boots": 3,
    "steelsword": 4,
    "silversword": 5,
    "crossbow": 6,
    "head": 7,
    "hair": 8,
}


def _equipment_module():
    from . import ui_equipment

    return ui_equipment


def _equipment_grid_size_params(size):
    columns, scale = _EQUIPMENT_ITEM_PICKER_GRID_SIZES.get(
        str(size or _EQUIPMENT_ITEM_PICKER_GRID_DEFAULT_SIZE),
        _EQUIPMENT_ITEM_PICKER_GRID_SIZES[_EQUIPMENT_ITEM_PICKER_GRID_DEFAULT_SIZE],
    )
    label_chars = max(10, 8 + (7 - columns) * 3)
    return columns, scale, label_chars


def _equipment_item_picker_search_blob(identifier, label, description, attrs=None, fallback_template=""):
    attrs = attrs if isinstance(attrs, dict) else {}
    parts = [
        identifier,
        label,
        description,
        fallback_template,
        attrs.get("item_name", ""),
        attrs.get("category", ""),
        attrs.get("equip_template", ""),
        attrs.get("hold_template", ""),
        attrs.get("template_name", ""),
        attrs.get("equip_slot", ""),
        attrs.get("hold_slot", ""),
        attrs.get("attachment_type", ""),
        attrs.get("attachment_prefix", ""),
    ]
    tags = attrs.get("tags", [])
    if isinstance(tags, str):
        parts.append(tags)
    else:
        try:
            parts.extend(str(tag) for tag in tags if tag)
        except Exception:
            pass
    return " ".join(str(part or "") for part in parts).lower()


def _equipment_item_picker_terms(search_text):
    return [term for term in str(search_text or "").strip().lower().split() if term]


def _equipment_item_picker_matches(identifier, label, description, attrs, fallback_template, search_terms):
    if not search_terms:
        return True
    blob = _equipment_item_picker_search_blob(identifier, label, description, attrs, fallback_template)
    return all(term in blob for term in search_terms)


def _equipment_item_recent_key(source_game, category):
    return (_normalize_source_game(source_game), str(category or "None"))


def _get_equipment_item_recents(source_game, category):
    return list(_EQUIPMENT_ITEM_PICKER_RECENTS.get(_equipment_item_recent_key(source_game, category), []))


def _remember_equipment_item_recent(source_game, category, item_name):
    item_name = str(item_name or "").strip()
    if not item_name:
        return
    key = _equipment_item_recent_key(source_game, category)
    recents = [name for name in _EQUIPMENT_ITEM_PICKER_RECENTS.get(key, []) if name != item_name]
    recents.insert(0, item_name)
    _EQUIPMENT_ITEM_PICKER_RECENTS[key] = recents[:_EQUIPMENT_ITEM_PICKER_RECENT_LIMIT]


def _sort_equipment_item_picker_rows(rows, source_game, category, sort_mode):
    sort_mode = str(sort_mode or "NAME_ASC")
    if sort_mode == "NAME_DESC":
        rows.sort(key=lambda row: str(row.get("label", "") or row.get("identifier", "")).lower(), reverse=True)
        return rows
    if sort_mode == "RECENT":
        recent_order = {
            item_name: index
            for index, item_name in enumerate(_get_equipment_item_recents(source_game, category))
        }
        rows.sort(
            key=lambda row: (
                recent_order.get(row.get("identifier", ""), 999999),
                str(row.get("label", "") or row.get("identifier", "")).lower(),
            )
        )
        return rows
    rows.sort(key=lambda row: str(row.get("label", "") or row.get("identifier", "")).lower())
    return rows


def _get_default_item_picker_rows(context, entry, source_game="w3", search_text="", sort_mode="NAME_ASC", limit=None):
    equipment = _equipment_module()
    rows = []
    match_count = 0
    all_count = 0
    search_terms = _equipment_item_picker_terms(search_text)

    try:
        items = entry.get_default_items(context)
    except Exception:
        items = [("None", "None", "")]

    for item in items or [("None", "None", "")]:
        all_count += 1
        identifier = str(item[0] or "None")
        label = str(item[1] or identifier)
        description = str(item[2] or "")
        attrs, fallback_template = equipment._get_equipment_item_attrs_for_enum(entry, identifier, source_game)
        if not _equipment_item_picker_matches(
            identifier,
            label,
            description,
            attrs,
            fallback_template,
            search_terms,
        ):
            continue
        match_count += 1
        rows.append({
            "identifier": identifier,
            "label": label,
            "description": description,
            "attrs": attrs,
            "fallback_template": fallback_template,
        })
    _sort_equipment_item_picker_rows(rows, source_game, getattr(entry, "category", "None"), sort_mode)
    if limit is not None:
        rows = rows[:max(0, int(limit))]
    return rows, match_count, all_count


def _set_equipment_default_item(context, entry_index, item_name):
    temp_data = getattr(context.window_manager, "witcherui_temp_data", None)
    if not temp_data or not (0 <= int(entry_index) < len(temp_data.equipment_entries)):
        return False
    entry = temp_data.equipment_entries[int(entry_index)]
    entry.defaultItemName = str(item_name or "None")
    _remember_equipment_item_recent(
        getattr(entry, "source_game", "") or _equipment_module()._get_temp_source_game(context),
        getattr(entry, "category", "None"),
        item_name,
    )
    try:
        if context.area:
            context.area.tag_redraw()
    except Exception:
        pass
    return True


def _encode_equipment_item_attrs(attrs):
    if not isinstance(attrs, dict):
        return "{}"
    try:
        return json.dumps(attrs, sort_keys=True, default=str)
    except Exception:
        return "{}"


def _decode_equipment_item_attrs(attrs_json):
    try:
        value = json.loads(str(attrs_json or "{}"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _equipment_item_picker_filter_token(entry, entry_index, source_game, search_text, sort_mode):
    try:
        category = getattr(entry, "category", "") or "None"
        item_name = getattr(entry, "defaultItemName", "") or "None"
    except Exception:
        category = "None"
        item_name = "None"
    return "\x1f".join((
        str(entry_index),
        _normalize_source_game(source_game),
        str(category),
        str(item_name),
        str(search_text or ""),
        str(sort_mode or "NAME_ASC"),
    ))


def _populate_equipment_item_picker_rows(context, entry, entry_index, source_game, search_text, sort_mode):
    temp_data = _equipment_module()._get_temp_equipment_data(context)
    if temp_data is None or entry is None:
        return 0, 0

    source_game = _normalize_source_game(source_game or getattr(entry, "source_game", "") or "w3")
    token = _equipment_item_picker_filter_token(entry, entry_index, source_game, search_text, sort_mode)
    if getattr(temp_data, "item_picker_filter_token", "") == token:
        return (
            int(getattr(temp_data, "item_picker_match_count", 0) or 0),
            int(getattr(temp_data, "item_picker_all_count", 0) or 0),
        )

    rows, match_count, all_count = _get_default_item_picker_rows(
        context,
        entry,
        source_game=source_game,
        search_text=search_text,
        sort_mode=sort_mode,
        limit=None,
    )
    current_name = str(getattr(entry, "defaultItemName", "") or "")
    active_index = -1

    previous_suppress = bool(getattr(temp_data, "item_picker_suppress_select", False))
    temp_data.item_picker_suppress_select = True
    try:
        temp_data.item_picker_rows.clear()
        for index, row_data in enumerate(rows):
            row = temp_data.item_picker_rows.add()
            identifier = str(row_data.get("identifier", "") or "None")
            label = str(row_data.get("label", "") or identifier)
            row.identifier = identifier
            row.label = label
            row.description = str(row_data.get("description", "") or "")
            row.fallback_template = str(row_data.get("fallback_template", "") or "")
            row.attrs_json = _encode_equipment_item_attrs(row_data.get("attrs", {}))
            row.source_game = source_game
            try:
                row.name = label
            except Exception:
                pass
            if identifier == current_name:
                active_index = index
        temp_data.item_picker_entry_index = int(entry_index)
        temp_data.item_picker_source_game = source_game
        temp_data.item_picker_match_count = int(match_count)
        temp_data.item_picker_all_count = int(all_count)
        temp_data.item_picker_filter_token = token
        temp_data.item_picker_index = active_index
    finally:
        temp_data.item_picker_suppress_select = previous_suppress

    return match_count, all_count


def _get_equipment_item_picker_active_row(temp_data):
    if temp_data is None:
        return None
    try:
        index = int(getattr(temp_data, "item_picker_index", -1))
    except Exception:
        index = -1
    if 0 <= index < len(temp_data.item_picker_rows):
        return temp_data.item_picker_rows[index]
    return None


def _on_equipment_item_picker_index_changed(self, context):
    if bool(getattr(self, "item_picker_suppress_select", False)):
        return
    active_row = _get_equipment_item_picker_active_row(self)
    if active_row is None:
        return
    try:
        entry_index = int(getattr(self, "item_picker_entry_index", -1))
    except Exception:
        entry_index = -1
    if entry_index < 0:
        return
    _set_equipment_default_item(context, entry_index, getattr(active_row, "identifier", "None"))


def _on_equipment_item_picker_filter_changed(self, context):
    try:
        if int(getattr(self, "item_picker_page", 0) or 0) != 0:
            self.item_picker_page = 0
    except Exception:
        pass


def _equipment_picker_short_label(text, max_chars=32):
    text = str(text or "")
    if len(text) <= max_chars:
        return text
    return text[:max(0, max_chars - 3)].rstrip() + "..."


def _equipment_preset_picker_terms(search_text):
    return _equipment_item_picker_terms(search_text)


def _inventory_preset_entry_sort_key(entry):
    category = _inventory_preset_entry_category(entry)
    return (
        _EQUIPMENT_PRESET_PICKER_CATEGORY_ORDER.get(category, 99),
        str(entry.get("item", "") or entry.get("equip_template", "") or "").lower(),
    )


def _inventory_preset_entry_category(entry):
    return str(entry.get("category", "") or "").lower()


def _inventory_preset_entries(preset):
    entries = preset.get("entries", []) if isinstance(preset, dict) else []
    if not isinstance(entries, list):
        return []
    return sorted([entry for entry in entries if isinstance(entry, dict)], key=_inventory_preset_entry_sort_key)


def _inventory_preset_preview_entries(entries):
    entries = sorted([entry for entry in entries if isinstance(entry, dict)], key=_inventory_preset_entry_sort_key)
    selected = []
    selected_ids = set()
    by_category = {}
    for entry in entries:
        by_category.setdefault(_inventory_preset_entry_category(entry), entry)

    def _add(entry):
        if not isinstance(entry, dict):
            return
        entry_id = id(entry)
        if entry_id in selected_ids:
            return
        selected.append(entry)
        selected_ids.add(entry_id)

    for category in _EQUIPMENT_PRESET_PICKER_PREVIEW_CATEGORIES:
        _add(by_category.get(category))

    for entry in entries:
        if len(selected) >= _EQUIPMENT_PRESET_PICKER_PREVIEW_COUNT:
            break
        _add(entry)

    while len(selected) < _EQUIPMENT_PRESET_PICKER_PREVIEW_COUNT:
        selected.append({})
    return selected[:_EQUIPMENT_PRESET_PICKER_PREVIEW_COUNT]


def _inventory_preset_main_entry(preset):
    entries = _inventory_preset_entries(preset)
    for entry in entries:
        if str(entry.get("category", "") or "").lower() == "armor":
            return entry
    return entries[0] if entries else {}


def _inventory_preset_entry_item(entry):
    item_name = str(entry.get("item", "") or "").strip()
    initializer = entry.get("initializer")
    if isinstance(initializer, dict):
        item_name = str(initializer.get("itemName", "") or initializer.get("item", "") or item_name).strip()
    return item_name


def _inventory_preset_entry_template(entry):
    return str(
        entry.get("equip_template", "")
        or entry.get("template", "")
        or entry.get("templateName", "")
        or ""
    ).strip()


def _inventory_preset_entry_entity_path(entry):
    return str(
        entry.get("w2_entity_path", "")
        or entry.get("bodypart_entity_path", "")
        or entry.get("entity_path", "")
        or ""
    ).strip()


def _inventory_preset_entry_source_game(entry, fallback="w3"):
    return _normalize_source_game(entry.get("source_game", fallback) if isinstance(entry, dict) else fallback)


def _inventory_preset_entry_attrs(entry, source_game="w3"):
    equipment = _equipment_module()
    item_name = _inventory_preset_entry_item(entry)
    template = _inventory_preset_entry_template(entry)
    source_game = _inventory_preset_entry_source_game(entry, source_game)
    attrs = {}
    try:
        if item_name:
            attrs = equipment.get_item_attributes_by_identifier(item_name, source_game=source_game, strict=True)
        if not attrs and template:
            attrs = equipment.get_item_attributes_by_identifier(template, source_game=source_game, strict=True)
    except Exception:
        attrs = {}
    if not isinstance(attrs, dict):
        attrs = {}
    return attrs


def _inventory_preset_detail_lines(entries):
    lines = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        category = str(entry.get("category", "") or "item").strip() or "item"
        item_name = _inventory_preset_entry_item(entry)
        template = _inventory_preset_entry_template(entry)
        bodypart_path = str(entry.get("bodypart_entity_path", "") or "").strip()
        w2_entity_path = str(entry.get("w2_entity_path", "") or "").strip()
        xml_path = str(entry.get("xml_source_path", "") or "").strip()
        details = []
        if item_name:
            details.append(item_name)
        if template:
            details.append(f"template={template}")
        if bodypart_path:
            details.append(f"bodypart={bodypart_path}")
        if w2_entity_path:
            details.append(f"entity={w2_entity_path}")
        if xml_path:
            details.append(f"xml={xml_path}")
        if not details:
            details.append("(empty)")
        lines.append(f"{category}: " + " | ".join(details))
    return lines


def _inventory_preset_details_text(entries):
    lines = _inventory_preset_detail_lines(entries)
    return "\n".join(lines) if lines else "(No saved items)"


def _encode_inventory_preset_entries(entries):
    try:
        return json.dumps(list(entries or []), sort_keys=False, default=str)
    except Exception:
        return "[]"


def _decode_inventory_preset_entries(entries_json):
    try:
        value = json.loads(str(entries_json or "[]"))
    except Exception:
        return []
    return [entry for entry in value if isinstance(entry, dict)] if isinstance(value, list) else []


def _inventory_preset_search_blob(preset):
    entries = _inventory_preset_entries(preset)
    parts = [
        preset.get("id", ""),
        preset.get("name", ""),
        preset.get("source", ""),
        preset.get("derived_from", {}).get("set_folder", "") if isinstance(preset.get("derived_from"), dict) else "",
    ]
    for entry in entries:
        parts.extend([
            entry.get("category", ""),
            entry.get("item", ""),
            _inventory_preset_entry_template(entry),
            entry.get("bodypart_entity_path", ""),
            entry.get("w2_entity_path", ""),
            entry.get("xml_source_path", ""),
        ])
    return " ".join(str(part or "") for part in parts).lower()


def _inventory_preset_matches(preset, search_terms):
    if not search_terms:
        return True
    blob = _inventory_preset_search_blob(preset)
    return all(term in blob for term in search_terms)


def _sort_inventory_preset_picker_rows(rows, sort_mode):
    sort_mode = str(sort_mode or "ORDER")
    if sort_mode == "NAME_ASC":
        rows.sort(key=lambda row: str(row.get("label", "") or row.get("identifier", "")).lower())
    elif sort_mode == "NAME_DESC":
        rows.sort(key=lambda row: str(row.get("label", "") or row.get("identifier", "")).lower(), reverse=True)
    return rows


def _get_inventory_preset_picker_rows(context, search_text="", sort_mode="ORDER", limit=None, source_game=None):
    equipment = _equipment_module()
    presets = equipment._load_inventory_presets(source_game=source_game)
    search_terms = _equipment_preset_picker_terms(search_text)
    rows = []
    match_count = 0
    for index, preset in enumerate(presets):
        if not _inventory_preset_matches(preset, search_terms):
            continue
        match_count += 1
        entries = _inventory_preset_entries(preset)
        main_entry = _inventory_preset_main_entry(preset)
        rows.append({
            "identifier": str(preset.get("id", "") or ""),
            "label": str(preset.get("name", "") or preset.get("id", "") or ""),
            "description": f"{preset.get('source', 'user')}, {preset.get('source_game', 'w3')}, {len(entries)} item(s)",
            "source": str(preset.get("source", "") or "user"),
            "is_shipped": bool(preset.get("is_shipped", False)),
            "source_game": _normalize_source_game(preset.get("source_game", "w3")),
            "entries": entries,
            "main_item": _inventory_preset_entry_item(main_entry),
            "main_template": _inventory_preset_entry_template(main_entry),
            "order": index,
        })
    _sort_inventory_preset_picker_rows(rows, sort_mode)
    if limit is not None:
        rows = rows[:max(0, int(limit))]
    return rows, match_count, len(presets)


def _inventory_preset_picker_filter_token(search_text, sort_mode, target="", source_game="w3"):
    return "\x1f".join((
        str(search_text or ""),
        str(sort_mode or "ORDER"),
        str(target or "INVENTORY"),
        _normalize_source_game(source_game),
    ))


def _inventory_preset_picker_target(context=None):
    temp_data = None
    if context is not None:
        temp_data = _equipment_module()._get_temp_equipment_data(context)
    if temp_data is None:
        try:
            temp_data = bpy.context.window_manager.witcherui_temp_data
        except Exception:
            temp_data = None
    return str(getattr(temp_data, "preset_picker_target", "INVENTORY") or "INVENTORY")


def _populate_inventory_preset_picker_rows(context, search_text, sort_mode):
    temp_data = _equipment_module()._get_temp_equipment_data(context)
    if temp_data is None:
        return 0, 0

    target = _inventory_preset_picker_target(context)
    source_game = _equipment_module()._inventory_preset_source_game_for_target(context, target)
    token = _inventory_preset_picker_filter_token(search_text, sort_mode, target, source_game)
    if getattr(temp_data, "preset_picker_filter_token", "") == token:
        return (
            int(getattr(temp_data, "preset_picker_match_count", 0) or 0),
            int(getattr(temp_data, "preset_picker_all_count", 0) or 0),
        )

    rows, match_count, all_count = _get_inventory_preset_picker_rows(
        context,
        search_text=search_text,
        sort_mode=sort_mode,
        source_game=source_game,
    )
    current_id = str(_equipment_module()._get_inventory_preset_selection(
        context,
        target=target,
    ) or "")
    active_index = -1
    temp_data.preset_picker_rows.clear()
    for index, row_data in enumerate(rows):
        row = temp_data.preset_picker_rows.add()
        identifier = str(row_data.get("identifier", "") or "")
        label = str(row_data.get("label", "") or identifier)
        row.identifier = identifier
        row.label = label
        row.description = str(row_data.get("description", "") or "")
        row.source = str(row_data.get("source", "") or "user")
        row.is_shipped = bool(row_data.get("is_shipped", False))
        row.source_game = _normalize_source_game(row_data.get("source_game", "w3"))
        row.entries_json = _encode_inventory_preset_entries(row_data.get("entries", []))
        row.main_item = str(row_data.get("main_item", "") or "")
        row.main_template = str(row_data.get("main_template", "") or "")
        try:
            row.name = label
        except Exception:
            pass
        if identifier == current_id:
            active_index = index
    temp_data.preset_picker_index = active_index
    temp_data.preset_picker_match_count = int(match_count)
    temp_data.preset_picker_all_count = int(all_count)
    temp_data.preset_picker_filter_token = token
    return match_count, all_count


def _on_equipment_preset_picker_filter_changed(self, context):
    try:
        if int(getattr(self, "preset_picker_page", 0) or 0) != 0:
            self.preset_picker_page = 0
    except Exception:
        pass


def _set_inventory_preset_selection(context, preset_id):
    preset_id = str(preset_id or "").strip()
    equipment = _equipment_module()
    target = _inventory_preset_picker_target(context)
    source_game = equipment._inventory_preset_source_game_for_target(context, target)
    if not preset_id or preset_id == "__none__":
        return bool(equipment._set_inventory_preset_selection(
            context,
            "",
            target=target,
        ))
    if equipment._get_inventory_preset(preset_id, source_game=source_game) is None:
        return False
    return bool(equipment._set_inventory_preset_selection(
        context,
        preset_id,
        target=target,
    ))


class EquipmentItemPickerRow(bpy.types.PropertyGroup):
    identifier: bpy.props.StringProperty(name="Identifier", default="")
    label: bpy.props.StringProperty(name="Name", default="")
    description: bpy.props.StringProperty(name="Description", default="")
    fallback_template: bpy.props.StringProperty(name="Template", default="")
    attrs_json: bpy.props.StringProperty(name="Attributes", default="{}")
    source_game: bpy.props.StringProperty(name="Source Game", default="w3")


class EquipmentPresetPickerRow(bpy.types.PropertyGroup):
    identifier: bpy.props.StringProperty(name="Identifier", default="")
    label: bpy.props.StringProperty(name="Name", default="")
    description: bpy.props.StringProperty(name="Description", default="")
    source: bpy.props.StringProperty(name="Source", default="user")
    is_shipped: bpy.props.BoolProperty(name="Shipped", default=False)
    source_game: bpy.props.StringProperty(name="Source Game", default="w3")
    entries_json: bpy.props.StringProperty(name="Entries", default="[]")
    main_item: bpy.props.StringProperty(name="Main Item", default="")
    main_template: bpy.props.StringProperty(name="Main Template", default="")


class EQUIPMENT_OT_PickDefaultItem(bpy.types.Operator):
    bl_idname = "witcher.equipment_pick_default_item"
    bl_label = "Pick Item"
    bl_description = "Pick this equipment item"
    bl_options = {'INTERNAL'}

    entry_index: bpy.props.IntProperty(default=-1, options={'HIDDEN'})
    item_name: bpy.props.StringProperty(default="", options={'HIDDEN'})
    tooltip: bpy.props.StringProperty(default="", options={'HIDDEN', 'SKIP_SAVE'})

    @classmethod
    def description(cls, context, properties):
        tip = str(getattr(properties, "tooltip", "") or "").strip()
        return tip or cls.bl_description

    def execute(self, context):
        if not _set_equipment_default_item(context, self.entry_index, self.item_name):
            return {'CANCELLED'}
        return {'FINISHED'}


class EQUIPMENT_OT_ItemPickerPage(bpy.types.Operator):
    bl_idname = "witcher.equipment_item_picker_page"
    bl_label = "Picker Page"
    bl_description = "Show the previous or next page of items"
    bl_options = {'INTERNAL'}

    direction: bpy.props.EnumProperty(
        items=[('PREV', "Previous", ""), ('NEXT', "Next", "")],
        default='NEXT',
        options={'HIDDEN'},
    )
    max_page: bpy.props.IntProperty(default=0, options={'HIDDEN'})

    def execute(self, context):
        temp_data = _equipment_module()._get_temp_equipment_data(context)
        if temp_data is None:
            return {'CANCELLED'}
        page = int(getattr(temp_data, "item_picker_page", 0) or 0)
        page += -1 if self.direction == 'PREV' else 1
        temp_data.item_picker_page = max(0, min(page, max(0, int(self.max_page))))
        return {'FINISHED'}


class EQUIPMENT_OT_PresetPickerPage(bpy.types.Operator):
    bl_idname = "witcher.equipment_preset_picker_page"
    bl_label = "Picker Page"
    bl_description = "Show the previous or next page of presets"
    bl_options = {'INTERNAL'}

    direction: bpy.props.EnumProperty(
        items=[('PREV', "Previous", ""), ('NEXT', "Next", "")],
        default='NEXT',
        options={'HIDDEN'},
    )
    max_page: bpy.props.IntProperty(default=0, options={'HIDDEN'})

    def execute(self, context):
        temp_data = _equipment_module()._get_temp_equipment_data(context)
        if temp_data is None:
            return {'CANCELLED'}
        page = int(getattr(temp_data, "preset_picker_page", 0) or 0)
        page += -1 if self.direction == 'PREV' else 1
        temp_data.preset_picker_page = max(0, min(page, max(0, int(self.max_page))))
        return {'FINISHED'}


class EQUIPMENT_OT_PickInventoryPreset(bpy.types.Operator):
    bl_idname = "witcher.equipment_pick_inventory_preset"
    bl_label = "Pick Preset"
    bl_description = "Pick this inventory preset"
    bl_options = {'INTERNAL'}

    preset_id: bpy.props.StringProperty(default="", options={'HIDDEN'})
    tooltip: bpy.props.StringProperty(default="", options={'HIDDEN', 'SKIP_SAVE'})

    @classmethod
    def description(cls, context, properties):
        tip = str(getattr(properties, "tooltip", "") or "").strip()
        return tip or cls.bl_description

    def execute(self, context):
        if not _set_inventory_preset_selection(context, self.preset_id):
            return {'CANCELLED'}
        return {'FINISHED'}


class EQUIPMENT_OT_DeleteInventoryPreset(bpy.types.Operator):
    bl_idname = "witcher.equipment_delete_inventory_preset"
    bl_label = "Delete Preset"
    bl_description = "Delete this user inventory preset"
    bl_options = {'INTERNAL'}

    preset_id: bpy.props.StringProperty(name="Preset ID", default="", options={'SKIP_SAVE'})
    preset_name: bpy.props.StringProperty(name="Name", default="", options={'SKIP_SAVE'})
    source_game: bpy.props.StringProperty(name="Game", default="", options={'SKIP_SAVE'})

    @classmethod
    def description(cls, context, properties):
        name = str(getattr(properties, "preset_name", "") or "").strip()
        return f"Delete user preset: {name}" if name else cls.bl_description

    def invoke(self, context, event):
        equipment = _equipment_module()
        preset_id = str(self.preset_id or "").strip()
        source_game = str(self.source_game or "").strip() or None
        preset = equipment._get_inventory_preset(preset_id, source_game=source_game)
        if not preset:
            self.report({'WARNING'}, "Preset not found.")
            return {'CANCELLED'}
        if bool(preset.get("is_shipped", False)):
            self.report({'WARNING'}, "Shipped presets cannot be deleted.")
            return {'CANCELLED'}
        self.preset_name = str(preset.get("name", "") or self.preset_name or preset_id)
        self.source_game = str(preset.get("source_game", "") or self.source_game or "")
        try:
            return context.window_manager.invoke_props_dialog(self, width=520, confirm_text="Delete")
        except TypeError:
            return context.window_manager.invoke_props_dialog(self, width=520)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        layout.label(text="Delete user preset?", icon='ERROR')
        layout.prop(self, "preset_name", text="Name")
        layout.prop(self, "preset_id", text="Preset ID")
        layout.prop(self, "source_game", text="Game")

    def execute(self, context):
        equipment = _equipment_module()
        preset_id = str(self.preset_id or "").strip()
        source_game = str(self.source_game or "").strip() or None
        preset = equipment._get_inventory_preset(preset_id, source_game=source_game)
        if not preset:
            self.report({'WARNING'}, "Preset not found.")
            return {'CANCELLED'}
        if bool(preset.get("is_shipped", False)):
            self.report({'WARNING'}, "Shipped presets cannot be deleted.")
            return {'CANCELLED'}
        removed = equipment._delete_user_inventory_preset(preset_id, context=context)
        if not removed:
            self.report({'WARNING'}, "User preset not found.")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Deleted inventory preset: {removed.get('name', preset_id)}")
        return {'FINISHED'}


class EQUIPMENT_OT_ShowInventoryPresetDetails(bpy.types.Operator):
    bl_idname = "witcher.equipment_inventory_preset_details"
    bl_label = "Inventory Preset Details"
    bl_description = "Show all items saved in this inventory preset"
    bl_options = {'INTERNAL'}

    preset_id: bpy.props.StringProperty(name="Preset ID", default="", options={'SKIP_SAVE'})
    preset_name: bpy.props.StringProperty(name="Name", default="", options={'SKIP_SAVE'})
    source: bpy.props.StringProperty(name="Source", default="", options={'SKIP_SAVE'})
    source_game: bpy.props.StringProperty(name="Game", default="w3", options={'SKIP_SAVE'})
    entries_json: bpy.props.StringProperty(default="[]", options={'HIDDEN', 'SKIP_SAVE'})
    item_count: bpy.props.IntProperty(name="Items", default=0, options={'SKIP_SAVE'})
    items_text: bpy.props.StringProperty(name="Saved Items", default="", options={'SKIP_SAVE'})

    def invoke(self, context, event):
        entries = _decode_inventory_preset_entries(self.entries_json)
        self.item_count = len(entries)
        self.items_text = _inventory_preset_details_text(entries)
        return context.window_manager.invoke_props_dialog(self, width=760)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        layout.prop(self, "preset_name", text="Name")
        layout.prop(self, "preset_id", text="Preset ID")
        layout.prop(self, "source", text="Source")
        layout.prop(self, "source_game", text="Game")
        layout.prop(self, "item_count", text="Items")
        layout.prop(self, "items_text", text="Saved Items")

    def execute(self, context):
        return {'FINISHED'}


def inventory_preset_picker_width():
    return _EQUIPMENT_PRESET_PICKER_WIDTH


def _inventory_preset_entry_icon_id(context, entry):
    item_name = _inventory_preset_entry_item(entry)
    template = _inventory_preset_entry_template(entry)
    entity_path = _inventory_preset_entry_entity_path(entry)
    source_game = _inventory_preset_entry_source_game(entry, "w3")
    attrs = _inventory_preset_entry_attrs(entry, source_game=source_game)
    if source_game == "w2":
        # W2 shipped presets currently have entity paths but no direct icon metadata.
        # Avoid resolving .w2ent previews here; that can stall Blender while opening the picker.
        raw_icon_path = str(entry.get("icon_path", "") or attrs.get("icon_path", "") or "").strip()
        if not raw_icon_path:
            return 0
        return _equipment_module()._get_cached_or_queue_equipment_item_icon_id(
            context,
            "",
            {"icon_path": raw_icon_path},
            source_game=source_game,
            fallback_template="",
        )
    return _equipment_module()._get_cached_or_queue_equipment_item_icon_id(
        context,
        item_name or template or entity_path,
        attrs,
        source_game=source_game,
        fallback_template=entity_path or template,
    )


def _draw_inventory_preset_pick_button(layout, preset_id, label, current, *, icon_value=None, tooltip=None):
    preset_id = str(preset_id or "")
    current = str(current or "")
    is_current = (preset_id == current)
    kwargs = {
        "text": label,
        "depress": is_current,
    }
    if icon_value is not None:
        kwargs["icon_value"] = icon_value
    op = layout.operator("witcher.equipment_pick_inventory_preset", **kwargs)
    op.preset_id = preset_id
    if tooltip:
        op.tooltip = str(tooltip or "")
    return op


def _draw_inventory_preset_delete_button(layout, row):
    if bool(getattr(row, "is_shipped", False)):
        return None
    preset_id = getattr(row, "identifier", "") or ""
    if not preset_id:
        return None
    op = layout.operator("witcher.equipment_delete_inventory_preset", text="", icon='X')
    op.preset_id = preset_id
    op.preset_name = getattr(row, "label", "") or preset_id
    op.source_game = getattr(row, "source_game", "") or ""
    return op


def _draw_inventory_preset_picker_grid(context, layout, rows, current, placeholder_id, columns, scale, label_chars):
    grid = layout.grid_flow(
        row_major=True,
        columns=columns,
        even_columns=True,
        even_rows=True,
        align=True,
    )
    for row in rows:
        preset_id = getattr(row, "identifier", "") or ""
        label = getattr(row, "label", "") or preset_id
        main_entry = {
            "item": getattr(row, "main_item", "") or "",
            "equip_template": getattr(row, "main_template", "") or "",
            "source_game": getattr(row, "source_game", "") or "w3",
        }
        icon_id = _inventory_preset_entry_icon_id(context, main_entry) or placeholder_id

        cell = grid.box().column(align=True)
        icon_row = cell.row(align=True)
        icon_row.alignment = 'CENTER'
        icon_row.template_icon(icon_value=icon_id, scale=scale)
        action_row = cell.row(align=True)
        _draw_inventory_preset_pick_button(
            action_row,
            preset_id,
            _equipment_picker_short_label(label, max_chars=label_chars),
            current,
            tooltip=label,
        )
        _draw_inventory_preset_delete_button(action_row, row)

    fill = (columns - (len(rows) % columns)) % columns
    for _ in range(fill):
        spacer = grid.column(align=True)
        spacer.enabled = False
        spacer.label(text="")


def _draw_inventory_preset_picker_list(context, layout, rows, current, placeholder_id):
    col = layout.column(align=True)
    for row in rows:
        preset_id = getattr(row, "identifier", "") or ""
        label = getattr(row, "label", "") or preset_id
        entries = _decode_inventory_preset_entries(getattr(row, "entries_json", "[]"))
        preview_entries = _inventory_preset_preview_entries(entries)

        line = col.box().row(align=True)
        icon_strip = line.row(align=True)
        icon_strip.alignment = 'LEFT'
        for entry in preview_entries:
            icon_id = _inventory_preset_entry_icon_id(context, entry) or placeholder_id
            icon_strip.template_icon(
                icon_value=icon_id,
                scale=_EQUIPMENT_PRESET_PICKER_LIST_ICON_SCALE,
            )

        label_col = line.column(align=True)
        _draw_inventory_preset_pick_button(
            label_col,
            preset_id,
            _equipment_picker_short_label(label, max_chars=_EQUIPMENT_PRESET_PICKER_LIST_LABEL_CHARS),
            current,
            tooltip=label,
        )
        info_op = line.operator(
            "witcher.equipment_inventory_preset_details",
            text="",
            icon='INFO',
        )
        info_op.preset_id = preset_id
        info_op.preset_name = label
        info_op.source = getattr(row, "source", "") or ""
        info_op.source_game = getattr(row, "source_game", "") or "w3"
        info_op.entries_json = getattr(row, "entries_json", "[]") or "[]"
        _draw_inventory_preset_delete_button(line, row)


def draw_inventory_preset_picker(context, layout):
    equipment = _equipment_module()
    temp_data = equipment._get_temp_equipment_data(context)
    if temp_data is None:
        layout.label(text="No temporary UI data", icon='ERROR')
        return

    is_grid = (temp_data.preset_picker_view == 'GRID')
    placeholder_id = equipment._get_equipment_placeholder_icon_id()
    search_text = str(temp_data.preset_picker_search or "")
    target = _inventory_preset_picker_target(context)
    grid_columns, grid_scale, _grid_label_chars = _equipment_grid_size_params(temp_data.preset_picker_grid_size)

    header = layout.row(align=True)
    header.prop(temp_data, "preset_picker_search", text="", icon='VIEWZOOM')
    header.prop(temp_data, "preset_picker_view", text="", icon_only=True, expand=True)
    if is_grid:
        header.prop(temp_data, "preset_picker_grid_size", text="", icon_only=True)
    header.prop(temp_data, "preset_picker_sort", text="")

    match_count, all_count = _populate_inventory_preset_picker_rows(
        context,
        search_text,
        temp_data.preset_picker_sort,
    )
    rows = list(temp_data.preset_picker_rows)
    total = len(rows)

    page_size = (
        grid_columns * _EQUIPMENT_PRESET_PICKER_GRID_PAGE_ROWS
        if is_grid
        else _EQUIPMENT_PRESET_PICKER_LIST_PAGE_ROWS
    )
    page_count = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(int(temp_data.preset_picker_page), page_count - 1))

    info = layout.row(align=True)
    count_box = info.row(align=True)
    count_box.alignment = 'LEFT'
    if search_text:
        count_box.label(text=f"{match_count} of {all_count} presets", icon='VIEWZOOM')
    else:
        count_box.label(text=f"{total} presets", icon='PRESET')

    current_id = str(equipment._get_inventory_preset_selection(context, target=target) or "")
    source_game = equipment._inventory_preset_source_game_for_target(context, target)
    current_label = equipment._inventory_preset_label(
        current_id,
        fallback="None",
        source_game=source_game,
    )
    current_box = info.row(align=True)
    current_box.alignment = 'CENTER'
    target_label = equipment._inventory_preset_target_label(target)
    current_box.label(text=target_label + ": " + _equipment_picker_short_label(current_label, max_chars=28))
    clear_box = info.row(align=True)
    clear_box.enabled = bool(current_id)
    clear_op = clear_box.operator("witcher.equipment_clear_inventory_preset", text="", icon='X')
    clear_op.target = target

    if page_count > 1:
        nav = layout.row(align=True)
        nav.alignment = 'CENTER'
        prev_op = nav.operator("witcher.equipment_preset_picker_page", text="", icon='TRIA_LEFT')
        prev_op.direction = 'PREV'
        prev_op.max_page = page_count - 1
        nav.label(text=f"Page {page + 1} / {page_count}")
        next_op = nav.operator("witcher.equipment_preset_picker_page", text="", icon='TRIA_RIGHT')
        next_op.direction = 'NEXT'
        next_op.max_page = page_count - 1

    if total == 0:
        empty = layout.box()
        empty.alert = True
        empty.label(text="No presets match your search.", icon='ERROR')
        return

    start = page * page_size
    page_rows = rows[start:start + page_size]
    body = layout.box()
    if is_grid:
        _draw_inventory_preset_picker_grid(
            context,
            body,
            page_rows,
            current_id,
            placeholder_id,
            grid_columns,
            grid_scale,
            _EQUIPMENT_PRESET_PICKER_GRID_LABEL_CHARS,
        )
    else:
        _draw_inventory_preset_picker_list(context, body, page_rows, current_id, placeholder_id)


class EQUIPMENT_OT_SearchDefaultItem(bpy.types.Operator):
    bl_idname = "witcher.equipment_search_default_item"
    bl_label = "Select Item"
    bl_description = "Search and pick an item for the selected category"

    entry_index: bpy.props.IntProperty(default=-1, options={'HIDDEN'})

    def _get_entry(self, context):
        temp_data = getattr(context.window_manager, "witcherui_temp_data", None)
        if temp_data and 0 <= self.entry_index < len(temp_data.equipment_entries):
            return temp_data.equipment_entries[self.entry_index]
        return None

    def invoke(self, context, event):
        equipment = _equipment_module()
        temp_data = equipment._get_temp_equipment_data(context)
        entry = self._get_entry(context)
        if temp_data is None or entry is None:
            return {'CANCELLED'}

        temp_data.item_picker_entry_index = int(self.entry_index)
        temp_data.item_picker_search = ""
        temp_data.item_picker_page = 0
        equipment._get_equipment_placeholder_icon_id()

        try:
            context.window_manager.invoke_props_dialog(
                self,
                width=_EQUIPMENT_ITEM_PICKER_WIDTH,
                confirm_text="Done",
            )
        except TypeError:
            context.window_manager.invoke_props_dialog(self, width=_EQUIPMENT_ITEM_PICKER_WIDTH)
        return {'RUNNING_MODAL'}

    @staticmethod
    def _row_icon_id(context, row):
        attrs = _decode_equipment_item_attrs(getattr(row, "attrs_json", "{}"))
        return _equipment_module()._get_cached_or_queue_equipment_item_icon_id(
            context,
            getattr(row, "identifier", ""),
            attrs,
            source_game=getattr(row, "source_game", "") or "w3",
            fallback_template=getattr(row, "fallback_template", ""),
        )

    def _pick_op(self, layout, identifier, *, text, current, icon_value=None, tooltip=None):
        kwargs = {"text": text, "depress": (identifier == current)}
        if icon_value is not None:
            kwargs["icon_value"] = icon_value
        op = layout.operator("witcher.equipment_pick_default_item", **kwargs)
        op.entry_index = self.entry_index
        op.item_name = identifier
        if tooltip:
            op.tooltip = tooltip
        return op

    def _draw_grid(self, context, layout, page_rows, current, placeholder_id, columns, scale, label_chars):
        grid = layout.grid_flow(
            row_major=True,
            columns=columns,
            even_columns=True,
            even_rows=True,
            align=True,
        )
        for row in page_rows:
            identifier = getattr(row, "identifier", "") or "None"
            label = getattr(row, "label", "") or identifier
            icon_id = self._row_icon_id(context, row) or placeholder_id

            cell = grid.box().column(align=True)
            icon_row = cell.row(align=True)
            icon_row.alignment = 'CENTER'
            icon_row.template_icon(icon_value=icon_id, scale=scale)
            self._pick_op(
                cell,
                identifier,
                text=_equipment_picker_short_label(label, max_chars=label_chars),
                current=current,
                tooltip=label,
            )

        fill = (columns - (len(page_rows) % columns)) % columns
        for _ in range(fill):
            spacer = grid.column(align=True)
            spacer.enabled = False
            spacer.label(text="")

    def _draw_list(self, context, layout, page_rows, current, placeholder_id):
        col = layout.column(align=True)
        for row in page_rows:
            identifier = getattr(row, "identifier", "") or "None"
            label = getattr(row, "label", "") or identifier
            icon_id = self._row_icon_id(context, row) or placeholder_id

            line = col.box().row(align=True)
            thumb = line.row(align=True)
            thumb.alignment = 'LEFT'
            thumb.template_icon(icon_value=icon_id, scale=_EQUIPMENT_ITEM_PICKER_LIST_ICON_SCALE)
            label_col = line.column(align=True)
            label_col.scale_y = _EQUIPMENT_ITEM_PICKER_LIST_ICON_SCALE
            self._pick_op(
                label_col,
                identifier,
                text=_equipment_picker_short_label(label, max_chars=_EQUIPMENT_ITEM_PICKER_LIST_LABEL_CHARS),
                current=current,
                tooltip=label,
            )

    def draw(self, context):
        equipment = _equipment_module()
        layout = self.layout
        layout.separator()
        temp_data = equipment._get_temp_equipment_data(context)
        entry = self._get_entry(context)
        if temp_data is None or entry is None:
            layout.label(text="No equipment entry selected", icon='ERROR')
            return

        source_game = getattr(entry, "source_game", "") or equipment._get_temp_source_game(context)
        is_grid = (temp_data.item_picker_view == 'GRID')
        placeholder_id = equipment._get_equipment_placeholder_icon_id()
        search_text = str(temp_data.item_picker_search or "")
        grid_columns, grid_scale, grid_label_chars = _equipment_grid_size_params(temp_data.item_picker_grid_size)

        header = layout.row(align=True)
        header.prop(temp_data, "item_picker_search", text="", icon='VIEWZOOM')
        header.prop(temp_data, "item_picker_view", text="", icon_only=True, expand=True)
        if is_grid:
            header.prop(temp_data, "item_picker_grid_size", text="", icon_only=True)
        header.prop(temp_data, "item_picker_sort", text="")
        header.prop(temp_data, "auto_apply_equipment_selection", text="", icon='FILE_REFRESH')

        match_count, all_count = _populate_equipment_item_picker_rows(
            context,
            entry,
            self.entry_index,
            source_game,
            search_text,
            temp_data.item_picker_sort,
        )
        rows = list(temp_data.item_picker_rows)
        total = len(rows)

        page_size = (
            grid_columns * _EQUIPMENT_ITEM_PICKER_GRID_PAGE_ROWS
            if is_grid
            else _EQUIPMENT_ITEM_PICKER_LIST_PAGE_ROWS
        )
        page_count = max(1, (total + page_size - 1) // page_size)
        page = max(0, min(int(temp_data.item_picker_page), page_count - 1))

        info = layout.row(align=True)
        count_box = info.row(align=True)
        count_box.alignment = 'LEFT'
        if search_text:
            count_box.label(text=f"{match_count} of {all_count} items", icon='VIEWZOOM')
        else:
            count_box.label(text=f"{total} items", icon='PRESET')

        current = str(getattr(entry, "defaultItemName", "") or "None")
        current_box = info.row(align=True)
        current_box.alignment = 'CENTER'
        current_box.label(text="Current: " + _equipment_picker_short_label(current, max_chars=28))

        action_box = info.row(align=True)
        action_box.alignment = 'RIGHT'
        import_default = str(getattr(entry, "import_default_item", "") or "None")
        default_btn = action_box.row(align=True)
        default_btn.enabled = bool(import_default) and import_default != current
        default_op = default_btn.operator(
            "witcher.equipment_pick_default_item", text="Default", icon='LOOP_BACK'
        )
        default_op.entry_index = self.entry_index
        default_op.item_name = import_default
        clear_op = action_box.operator("witcher.equipment_pick_default_item", text="None", icon='X')
        clear_op.entry_index = self.entry_index
        clear_op.item_name = "None"

        if page_count > 1:
            nav = layout.row(align=True)
            nav.alignment = 'CENTER'
            prev_op = nav.operator("witcher.equipment_item_picker_page", text="", icon='TRIA_LEFT')
            prev_op.direction = 'PREV'
            prev_op.max_page = page_count - 1
            nav.label(text=f"Page {page + 1} / {page_count}")
            next_op = nav.operator("witcher.equipment_item_picker_page", text="", icon='TRIA_RIGHT')
            next_op.direction = 'NEXT'
            next_op.max_page = page_count - 1

        if total == 0:
            empty = layout.box()
            empty.alert = True
            empty.label(text="No items match your search.", icon='ERROR')
            return

        start = page * page_size
        page_rows = rows[start:start + page_size]

        body = layout.box()
        if is_grid:
            self._draw_grid(
                context, body, page_rows, current, placeholder_id,
                grid_columns, grid_scale, grid_label_chars,
            )
        else:
            self._draw_list(context, body, page_rows, current, placeholder_id)

    def execute(self, context):
        return {'FINISHED'}
