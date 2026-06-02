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


class EquipmentItemPickerRow(bpy.types.PropertyGroup):
    identifier: bpy.props.StringProperty(name="Identifier", default="")
    label: bpy.props.StringProperty(name="Name", default="")
    description: bpy.props.StringProperty(name="Description", default="")
    fallback_template: bpy.props.StringProperty(name="Template", default="")
    attrs_json: bpy.props.StringProperty(name="Attributes", default="{}")
    source_game: bpy.props.StringProperty(name="Source Game", default="w3")


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
