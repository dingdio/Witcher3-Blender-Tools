"""Blender node-editor UI for Witcher materials."""

import logging
from types import SimpleNamespace

import bpy

from ... import get_all_addon_prefs
from ..chain import coerce_source_index
from ..material import get_active_witcher_group_node
from ..vector_param import (
    get_legacy_w_value,
    get_mapping_vector_input,
    is_vector_param_node,
    mark_vector_param_node,
)
from .domain import (
    _base_path_group_recommendation,
    _base_read_is_stale,
    _base_read_item_matches_value_filters,
    _chain_item_for_entry,
    _compact_param_type_label,
    _display_type_for_linked_socket,
    _export_param_type_group,
    _find_base_read_item_for_socket,
    _get_live_base_read_snapshot_state,
    _is_base_read_auto_create_entry,
    _is_user_created_linked_node,
    _iter_local_nodes,
    _linked_item_type_validation_issue,
    _linked_item_uses_user_node,
    _linked_primary_for_param_name,
    _linked_socket_type_validation_issue,
    _material_source_game,
    _node_bool_prop,
    _node_string_prop,
    _normalize_texture_repo_path,
    _short_path_label,
    _source_file_label,
    _source_kind_label,
    _status_icon,
    _status_label,
    _texture_node_export_path,
    get_group_inputs,
    get_texarray_group_value,
    is_auxiliary_material_display_link,
    is_node_export_enabled,
    is_path_resolved,
)


log = logging.getLogger(__name__)


class WITCH_PT_materials(bpy.types.Panel):
    bl_label = "Witcher"
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = "Witcher"

    def _draw_copy_open_path_row(self, layout, path_text: str, *, icon='FILE', label="Path", prop_owner=None, prop_name: str = ""):
        path_text = str(path_text or "")
        if not path_text:
            return
        path_row = layout.row(align=True)
        path_row.label(text="", icon=icon)
        if prop_owner is not None and prop_name:
            path_row.prop(prop_owner, prop_name, text="")
            path_text = _node_string_prop(prop_owner, prop_name) or path_text
        else:
            path_row.label(text=f"{label}: {_short_path_label(path_text, 96)}")
        copy_op = path_row.operator("witcher.copy_texture_path", text="", icon='COPYDOWN')
        copy_op.path = path_text
        open_op = path_row.operator("witcher.open_base_material_chain_location", text="", icon='FILEBROWSER')
        open_op.source_path = path_text

    def _draw_texture_repo_path_row(
            self,
            layout,
            mat,
            input_socket,
            texture_node,
            *,
            path_prop: str,
            manual_prop: str,
            path_text: str,
            icon='FILE',
            ):
        path_text = _normalize_texture_repo_path(path_text)
        manual_enabled = _node_bool_prop(texture_node, manual_prop)
        resolved = bool(path_text and is_path_resolved(path_text))
        path_row = layout.row(align=True)
        path_row.label(text="", icon='CHECKMARK' if resolved else 'ERROR')
        path_row.prop(texture_node, manual_prop, text="", icon='EDITMODE_HLT', toggle=True)
        auto_op = path_row.operator("witcher.autoresolve_texture_repo_path", text="", icon='FILE_REFRESH')
        auto_op.param_name = str(getattr(input_socket, "name", "") or "")
        if manual_enabled:
            path_row.prop(texture_node, path_prop, text="")
            path_text = _normalize_texture_repo_path(_node_string_prop(texture_node, path_prop))
        else:
            path_row.label(text=f"Path: {_short_path_label(path_text, 96)}" if path_text else "Path: unresolved", icon=icon)
        if path_text:
            copy_op = path_row.operator("witcher.copy_texture_path", text="", icon='COPYDOWN')
            copy_op.path = path_text
            open_op = path_row.operator("witcher.open_base_material_chain_location", text="", icon='FILEBROWSER')
            open_op.source_path = path_text

    def _export_param_entry_for_socket(self, mat, props, node_ng, input_socket, index: int):
        if not input_socket.is_linked or not input_socket.links:
            return None
        linked_socket = input_socket.links[0].from_socket
        linked_node = linked_socket.node
        if is_auxiliary_material_display_link(input_socket, linked_socket, linked_node):
            return None
        promoted = bool(getattr(linked_node, "witcher_include", False))
        user_linked = _is_user_created_linked_node(linked_node)
        if not promoted and not user_linked:
            return None

        item = _find_base_read_item_for_socket(props, node_ng, input_socket)
        type_label = _display_type_for_linked_socket(input_socket, linked_node, item)
        group_key, group_label, group_icon, group_order = _export_param_type_group(input_socket, linked_node, item, store=False)
        return SimpleNamespace(
            index=index,
            input_socket=input_socket,
            linked_socket=linked_socket,
            linked_node=linked_node,
            promoted=promoted,
            user_linked=user_linked,
            item=item,
            type_label=type_label,
            label=f"{input_socket.name} ({type_label})" if type_label else input_socket.name,
            type_issue=_linked_socket_type_validation_issue(mat, input_socket, linked_node, store=False),
            group_key=group_key,
            group_label=group_label,
            group_icon=group_icon,
            group_order=group_order,
        )

    def _draw_export_param_entry(self, layout, mat, entry):
        linked_node = entry.linked_node
        linked_socket = entry.linked_socket
        input_socket = entry.input_socket
        target = layout.box() if entry.group_key == 'TEXTURE' else layout
        row = target.row(align=True)
        select_op = row.operator(
            "witcher.select_base_material_param_node",
            text="",
            icon='RESTRICT_SELECT_OFF',
            emboss=False,
        )
        select_op.param_name = input_socket.name
        if entry.user_linked:
            row.label(text="", icon='USER')
        else:
            row.label(text="", icon='BLANK1')
        if entry.type_issue:
            row.label(text="", icon='ERROR')
        else:
            row.label(text="", icon='BLANK1')
        if entry.promoted:
            row.prop(linked_node, "witcher_export", text=entry.label)
        else:
            promote_op = row.operator(
                "witcher.promote_base_material_param_to_local",
                text="",
                icon='ADD',
            )
            promote_op.param_name = input_socket.name
            row.label(text=entry.label)

        if linked_node.type == 'TEX_IMAGE':
            image_row = target.row(align=True)
            image_row.label(text="", icon='IMAGE_DATA')
            image_row.prop(linked_node, "image", text="")
            self._draw_texture_repo_path_row(
                target,
                mat,
                input_socket,
                linked_node,
                path_prop="witcher_texture_source_path",
                manual_prop="witcher_texture_path_manual",
                path_text=_texture_node_export_path(linked_node, mat, store=False),
                icon='IMAGE_DATA',
            )
        elif linked_node.type == 'GROUP':
            self._draw_texture_repo_path_row(
                target,
                mat,
                input_socket,
                linked_node,
                path_prop="witcher_texarray_source_path",
                manual_prop="witcher_texarray_path_manual",
                path_text=get_texarray_group_value(linked_node, store=False),
                icon='FILE',
            )
        elif linked_node.type == 'TEX_ENVIRONMENT':
            self._draw_texture_repo_path_row(
                target,
                mat,
                input_socket,
                linked_node,
                path_prop="witcher_texture_source_path",
                manual_prop="witcher_texture_path_manual",
                path_text=_texture_node_export_path(linked_node, mat, store=False),
                icon='FILE',
            )
        elif linked_node.type == 'RGB':
            row.prop(linked_socket, "default_value", text="")
        elif linked_node.type == 'VALUE':
            row.prop(linked_socket, "default_value", text="")
        elif input_socket.type == 'VECTOR':
            vector_node = linked_node
            if vector_node.type == 'MAPPING':
                vector_input = get_mapping_vector_input(vector_node, input_socket.name)
                if vector_input is not None:
                    row.prop(vector_input, "default_value", index=0, text="")
                    row.prop(vector_input, "default_value", index=1, text="")
                    row.prop(vector_input, "default_value", index=2, text="")
            elif vector_node.type == 'COMBXYZ':
                row.prop(vector_node.inputs[0], "default_value", text="")
                row.prop(vector_node.inputs[1], "default_value", text="")
                row.prop(vector_node.inputs[2], "default_value", text="")
            else:
                row.label(text=vector_node.bl_label or vector_node.type)
            if is_vector_param_node(vector_node):
                if not getattr(vector_node, "witcher_param_kind", ""):
                    legacy_w = get_legacy_w_value(input_socket, None)
                    if legacy_w is not None:
                        mark_vector_param_node(vector_node, input_socket.name, legacy_w)
                row.prop(vector_node, "witcher_vector_w", text="")
        else:
            row.prop(linked_socket, "default_value", text="")

    def _draw_base_path_controls(self, layout, mat):
        props = mat.witcher_props
        row = layout.row(align=True)
        row.prop(props, "base_custom", text="Base Path")
        search_op = row.operator("witcher.search_base_material_path", text="", icon='VIEWZOOM')
        search_op.source_game = _material_source_game(mat)
        row.operator("witcher.read_base_material", text="Load", icon='FILE_REFRESH')

        recommendation = _base_path_group_recommendation(mat)
        if not recommendation:
            return

        suggested_row = layout.row(align=True)
        suggested_row.scale_y = 0.9
        suggested_row.label(text=f"Suggested Group: {recommendation['node_group_name']}", icon='NODETREE')
        if recommendation.get("shader_type"):
            suggested_row.label(text=recommendation["shader_type"])

        if recommendation.get("has_active_group") and not recommendation.get("matches_current"):
            mismatch_row = layout.row(align=True)
            mismatch_row.alert = True
            mismatch_row.label(
                text=f"Current Group: {recommendation.get('current_tree_name') or recommendation.get('current_group_name') or 'None'}",
                icon='ERROR',
            )
            mismatch_row.operator("witcher.use_recommended_base_material_group", text="Use Recommended Group", icon='FILE_REFRESH')

    def _draw_base_read_items(self, layout, mat, items, *, action_enabled: bool):
        props = mat.witcher_props
        for stored_item, item in items:
            row = layout.row(align=True)
            row.prop(
                stored_item,
                "show_details",
                icon="TRIA_DOWN" if stored_item.show_details else "TRIA_RIGHT",
                icon_only=True,
                emboss=False,
            )
            status = str(getattr(item, "status", "") or "")
            param_name = str(getattr(item, "name", "") or "")
            user_linked = _linked_item_uses_user_node(mat, item)
            type_issue = _linked_item_type_validation_issue(mat, item, store=False) if bool(getattr(item, "is_linked", False)) else ""
            if str(getattr(item, "status", "") or "") == "present_linked":
                select_op = row.operator(
                    "witcher.select_base_material_param_node",
                    text="",
                    icon='CHECKMARK',
                    emboss=False,
                )
                select_op.param_name = item.name
            else:
                row.label(text="", icon=_status_icon(item))

            if user_linked:
                row.label(text="", icon='USER')
            elif type_issue:
                row.label(text="", icon='ERROR')
            else:
                row.label(text="", icon='BLANK1')

            input_socket, primary_node = _linked_primary_for_param_name(mat, item.name)
            promoted = bool(primary_node is not None and getattr(primary_node, "witcher_include", False))
            export_enabled = bool(promoted and is_node_export_enabled(primary_node))
            chain_item = _chain_item_for_entry(props, item)
            swatch_col = row.row(align=True)
            swatch_col.scale_x = 0.45
            if promoted:
                swatch_col.prop(props, "base_read_local_color", text="")
            elif chain_item is not None:
                swatch_col.prop(chain_item, "node_color", text="")
            else:
                swatch_col.label(text="", icon='BLANK1')

            if bool(getattr(item, "is_linked", False)):
                promote_op = row.operator(
                    "witcher.promote_base_material_param_to_local",
                    text="",
                    icon='CHECKMARK' if promoted else 'ADD',
                    depress=promoted,
                )
                promote_op.param_name = item.name

                if user_linked and item.has_value and item.is_supported:
                    replace_op = row.operator(
                        "witcher.replace_user_material_param_with_chain",
                        text="",
                        icon='FILE_REFRESH',
                    )
                    replace_op.param_name = item.name

            elif action_enabled and item.can_create:
                op = row.operator(
                    "witcher.create_base_material_param",
                    text="",
                    icon='ADD' if item.has_matching_socket else 'LINKED',
                )
                op.param_name = item.name
                op.create_export_socket = not item.has_matching_socket
            else:
                row.label(text="", icon='BLANK1')

            row.separator(factor=0.35)
            type_label = _compact_param_type_label(item.param_type) if item.param_type else ""
            label = f"{param_name} ({type_label})" if type_label else param_name
            if status != "present_linked" and not item.can_create:
                status_text = _status_label(item)
                if status_text:
                    label = f"{label} ({status_text})"
            row.label(text=label)

            if not stored_item.show_details:
                continue

            details = layout.column(align=True)
            details.scale_y = 0.9
            if promoted:
                export_state = "Local export" if export_enabled else "Local, export disabled"
            else:
                export_state = "Chain value"
            details.label(text=f"Export: {export_state}", icon='CHECKMARK' if promoted else 'LINKED')
            if user_linked:
                details.label(text="Linked node: user-created", icon='USER')
            if type_issue:
                details.label(text=type_issue, icon='ERROR')
            if user_linked and input_socket is not None and primary_node is not None:
                if getattr(primary_node, "type", "") == 'TEX_IMAGE':
                    self._draw_texture_repo_path_row(
                        details,
                        mat,
                        input_socket,
                        primary_node,
                        path_prop="witcher_texture_source_path",
                        manual_prop="witcher_texture_path_manual",
                        path_text=_texture_node_export_path(primary_node, mat, store=False),
                        icon='IMAGE_DATA',
                    )
                elif getattr(primary_node, "type", "") == 'GROUP':
                    self._draw_texture_repo_path_row(
                        details,
                        mat,
                        input_socket,
                        primary_node,
                        path_prop="witcher_texarray_source_path",
                        manual_prop="witcher_texarray_path_manual",
                        path_text=get_texarray_group_value(primary_node, store=False),
                        icon='FILE',
                    )
                elif getattr(primary_node, "type", "") == 'TEX_ENVIRONMENT':
                    self._draw_texture_repo_path_row(
                        details,
                        mat,
                        input_socket,
                        primary_node,
                        path_prop="witcher_texture_source_path",
                        manual_prop="witcher_texture_path_manual",
                        path_text=_texture_node_export_path(primary_node, mat, store=False),
                        icon='FILE',
                    )
            if item.value:
                value_text = str(item.value)
                value_row = details.row(align=True)
                value_row.label(text=f"Value: {_short_path_label(value_text, 96)}")
                copy_op = value_row.operator("witcher.copy_texture_path", text="", icon='COPYDOWN')
                copy_op.path = value_text
            source_file = _source_file_label(getattr(item, "source_path", ""))
            if item.source_kind or source_file:
                source_label = _source_kind_label(item.source_kind) if item.source_kind else "Source"
                if source_file:
                    source_label = f"{source_label}: {source_file}"
                details.label(text=source_label, icon='FILE')
            if item.source_path:
                self._draw_copy_open_path_row(details, str(item.source_path), icon='FILE', label="Path")
            if item.message:
                details.label(text=item.message, icon='INFO')

    def _draw_base_read_chain(self, layout, mat, props):
        chain_items = list(getattr(props, "base_read_chain", []) or [])
        if not chain_items and not props.base_read_chain_text:
            return

        chain_col = layout.column(align=True)
        chain_col.scale_y = 0.9
        header = chain_col.row(align=True)
        header.label(text="Sources", icon='LINKED')
        header.operator("witcher.material_chain_help", text="", icon='INFO')
        header.operator("witcher.layout_base_material_chain_nodes", text="", icon='SORT_ASC')
        header.operator("witcher.sort_base_material_chain_nodes", text="", icon='SORT_DESC')
        header.operator(
            "witcher.frame_base_material_chain_nodes",
            text="",
            icon='NODETREE',
            depress=bool(getattr(props, "base_read_chain_frames_enabled", True)),
        )
        header.operator("witcher.promote_selected_material_node_to_local", text="", icon='ADD')
        local_nodes = list(_iter_local_nodes(mat, linked_only=True) or [])
        if local_nodes:
            local_row = chain_col.row(align=True)
            local_row.operator(
                "witcher.select_base_material_local_nodes",
                text="",
                icon='RESTRICT_SELECT_OFF',
            )
            swatch = local_row.row(align=True)
            swatch.scale_x = 0.45
            swatch.prop(props, "base_read_local_color", text="")
            local_row.label(text=f"Local: {len(local_nodes)} node(s)", icon='CHECKMARK')
        if chain_items:
            for item in chain_items:
                source_kind = _source_kind_label(getattr(item, "source_kind", ""))
                path = str(getattr(item, "path", "") or "")
                row = chain_col.row(align=True)
                op = row.operator(
                    "witcher.select_base_material_chain_nodes",
                    text="",
                    icon='RESTRICT_SELECT_OFF',
                )
                op.source_path = path
                op.source_index = coerce_source_index(getattr(item, "source_index", -1))
                swatch = row.row(align=True)
                swatch.scale_x = 0.45
                swatch.prop(item, "node_color", text="")
                copy_op = row.operator("witcher.copy_texture_path", text="", icon='COPYDOWN')
                copy_op.path = path
                open_op = row.operator("witcher.open_base_material_chain_location", text="", icon='FILEBROWSER')
                open_op.source_path = path
                row.label(text=_short_path_label(f"{source_kind}: {path}", 100))
        else:
            for line in props.base_read_chain_text.splitlines():
                chain_col.label(text=_short_path_label(line, 100))

    def _draw_base_read_section(self, layout, context, mat):
        props = mat.witcher_props
        if not props.base_read_status:
            empty_row = layout.row()
            empty_row.label(text="Material Chain not loaded.", icon='INFO')
            empty_row.operator("witcher.load_base_material_snapshot", text="Load Snapshot", icon='FILE_REFRESH')
            return
        try:
            stored_items = list(props.base_read_params)
            live_items, live_counts = _get_live_base_read_snapshot_state(mat)
            items = [
                (stored_item, live_items[idx] if idx < len(live_items) else stored_item)
                for idx, stored_item in enumerate(stored_items)
            ]
            stale = _base_read_is_stale(props)
            material_ready = bool(get_active_witcher_group_node(mat))
            available_count = sum(
                1 for _, item in items
                if _is_base_read_auto_create_entry(item)
            )
            counts_text = (
                f"Linked {live_counts['present']}"
                f" | Missing {available_count}"
                f" | Export-only {live_counts['unsupported']}"
                f" | Declared {live_counts['declared_only']}"
            )

            snapshot_box = layout.box()
            header_row = snapshot_box.row(align=True)
            header_row.prop(
                props,
                "base_read_show_inspector",
                icon="TRIA_DOWN" if props.base_read_show_inspector else "TRIA_RIGHT",
                icon_only=True,
                emboss=False,
            )
            alert_text = ""
            if props.base_read_status == "error":
                header_row.label(text="Read failed", icon='ERROR')
                alert_text = props.base_read_message or "Material Chain read failed."
            elif stale:
                header_row.label(text="Snapshot is stale", icon='ERROR')
                alert_text = "Base Path changed; read again."
            else:
                header_row.label(text="Snapshot loaded", icon='CHECKMARK')
            header_row.label(text="Material Chain")
            header_row.label(text=counts_text)

            action_row = header_row.row(align=True)
            action_row.enabled = (
                props.base_read_status == "ok"
                and not stale
                and material_ready
                and any(
                    _is_base_read_auto_create_entry(item)
                    for _, item in items
                )
            )
            action_row.operator("witcher.create_missing_base_material_params", text="Create Missing", icon='ADD')

            if not props.base_read_show_inspector:
                return

            if alert_text:
                alert_row = snapshot_box.row()
                alert_row.alert = True
                alert_row.label(text=alert_text, icon='ERROR')

            self._draw_base_read_chain(snapshot_box, mat, props)

            info_row = snapshot_box.row(align=True)
            info_row.prop(
                props,
                "base_read_show_info",
                icon="TRIA_DOWN" if props.base_read_show_info else "TRIA_RIGHT",
                icon_only=True,
                emboss=False,
            )
            info_row.label(text="Info")
            if props.base_read_show_info:
                info_col = snapshot_box.column(align=True)
                info_col.scale_y = 0.9
                if props.base_read_message:
                    info_col.label(text=props.base_read_message, icon='INFO')
                if props.base_read_requested_path:
                    info_col.label(text=f"Requested: {props.base_read_requested_path}")
                if props.base_read_resolved_graph:
                    info_col.label(text=f"Resolved Graph: {props.base_read_resolved_graph}")
                if props.base_read_chain_text:
                    info_col.label(text="Chain:", icon='LINKED')
                    for line in props.base_read_chain_text.splitlines():
                        info_col.label(text=line)
                if props.base_read_count_created:
                    info_col.label(text=f"Last Created {props.base_read_count_created}")

            action_enabled = props.base_read_status == "ok" and not stale and material_ready
            values_row = snapshot_box.row(align=True)
            values_row.label(text="Values", icon='NODE')
            filter_row = snapshot_box.row(align=True)
            filter_row.label(text="", icon='VIEWZOOM')
            filter_row.prop(props, "base_read_value_search", text="")
            filter_row.prop(props, "base_read_value_type_filter", text="")

            filtered_items = [
                (stored_item, item)
                for stored_item, item in items
                if _base_read_item_matches_value_filters(
                    item,
                    props.base_read_value_search,
                    props.base_read_value_type_filter,
                )
            ]
            if len(filtered_items) != len(items):
                count_row = snapshot_box.row(align=True)
                count_row.scale_y = 0.85
                count_row.label(text=f"{len(filtered_items)} of {len(items)} shown", icon='FILTER')
            if filtered_items:
                self._draw_base_read_items(snapshot_box, mat, filtered_items, action_enabled=action_enabled)
            else:
                snapshot_box.label(text="No values match the current filter.", icon='INFO')
        except Exception:
            log.exception("Failed to draw Material Chain UI for material '%s'", getattr(mat, "name", "<unknown>"))
            error_row = layout.row()
            error_row.label(text="Material Chain UI error. See console for details.", icon='ERROR')

    def _draw_material_socket_controls(self, layout, mat):
        box = layout.box()
        group_inputs = get_group_inputs(mat)
        if not group_inputs:
            box.label(text="No active Witcher shader group inputs found.", icon='INFO')
            return

        header = box.row(align=True)
        header.label(text="Export Params", icon='CHECKMARK')
        header.operator("witcher.validate_material_export_params", text="", icon='CHECKMARK')
        header.operator("witcher.select_base_material_local_nodes", text="", icon='RESTRICT_SELECT_OFF')
        header.operator("witcher.promote_selected_material_node_to_local", text="", icon='ADD')

        props = mat.witcher_props
        sort_row = box.row(align=True)
        sort_row.scale_y = 0.9
        sort_row.label(text="Sort", icon='SORT_ASC')
        sort_row.prop(props, "export_params_sort_mode", text="")

        node_ng = get_active_witcher_group_node(mat)
        entries = [
            entry for index, input_socket in enumerate(group_inputs)
            for entry in [self._export_param_entry_for_socket(mat, props, node_ng, input_socket, index)]
            if entry is not None
        ]
        displayed_count = len(entries)
        local_count = sum(1 for entry in entries if entry.promoted)
        user_candidate_count = sum(1 for entry in entries if entry.user_linked and not entry.promoted)

        if props.export_params_sort_mode == 'TYPE':
            entries.sort(key=lambda entry: (entry.group_order, entry.label.lower(), entry.index))
            current_group = None
            for entry in entries:
                if entry.group_key != current_group:
                    group_entries = [candidate for candidate in entries if candidate.group_key == entry.group_key]
                    group_row = box.row(align=True)
                    group_row.scale_y = 0.85
                    group_row.label(text=f"{entry.group_label} ({len(group_entries)})", icon=entry.group_icon)
                    current_group = entry.group_key
                self._draw_export_param_entry(box, mat, entry)
        else:
            for entry in entries:
                self._draw_export_param_entry(box, mat, entry)

        if displayed_count == 0:
            box.label(text="No local params. Promote values from Material Chain.", icon='INFO')
        elif local_count == 0 and user_candidate_count:
            box.label(text="User-linked params are not exported until promoted.", icon='INFO')

    def draw(self, context):
        layout = self.layout
        mat = context.material
        if not (mat and mat.witcher_props):
            return

        box = layout.box()
        row = box.row(align=False)
        row.prop(mat.witcher_props, "witcher_material_settings_collapse", icon="TRIA_DOWN" if not mat.witcher_props.witcher_material_settings_collapse else "TRIA_RIGHT", icon_only=True, emboss=False)
        row.label(text="Global Settings")

        if not mat.witcher_props.witcher_material_settings_collapse:
            addon_prefs = get_all_addon_prefs(context)
            box.prop(addon_prefs, "mod_directory")
            box.label(text="Texture Root Paths:")
            row = box.row()
            col = row.column()
            col.template_list(
                "WITCHER_UL_path_list",
                "",
                addon_prefs, "path_list",
                addon_prefs, "active_path_index"
            )
            col = row.column()
            top = col.column(align=True)
            top.operator("witcher.add_path", text="", icon="ADD")
            top.operator("witcher.remove_path", text="", icon="REMOVE")
            if addon_prefs.path_list and 0 <= addon_prefs.active_path_index < len(addon_prefs.path_list):
                selected_item = addon_prefs.path_list[addon_prefs.active_path_index]
                box.prop(selected_item, "path", text="Selected Path")

        box = layout.box()
        box.prop(mat.witcher_props, "override_texture_root", text="Override Texture Root")
        row = box.row()
        row.enabled = mat.witcher_props.override_texture_root
        row.prop(mat.witcher_props, "custom_texture_root", text="Texture Root")
        box.operator("witcher.replace_principled_bsdf", text="Replace Principled BSDF")

        layout.prop(mat.witcher_props, "bind_name")
        row = layout.row()
        row.enabled = not mat.witcher_props.bind_name
        row.prop(mat.witcher_props, "name", text="Name")
        layout.prop(mat.witcher_props, "material_version")
        layout.prop(mat.witcher_props, "local")
        layout.prop(mat.witcher_props, "enableMask")
        self._draw_base_path_controls(layout, mat)

        if mat.witcher_props.local:
            tab_row = layout.row(align=True)
            tab_row.prop_enum(mat.witcher_props, "material_ui_tab", 'EXPORT')
            tab_row.prop_enum(mat.witcher_props, "material_ui_tab", 'BASE')

            if mat.witcher_props.material_ui_tab == 'EXPORT':
                self._draw_material_socket_controls(layout, mat)
                if mat.witcher_props.xml_text:
                    layout.prop(mat.witcher_props, "xml_text", text="Local Instance XML", expand=True)
            else:
                self._draw_base_read_section(layout, context, mat)
        else:
            self._draw_base_read_section(layout, context, mat)



class WITCH_UL_base_material_paths(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        layout.label(text=getattr(item, "path", "") or "", icon='FILE')


UI_CLASSES = (
    WITCH_UL_base_material_paths,
    WITCH_PT_materials,
)


def register():
    for cls in UI_CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(UI_CLASSES):
        bpy.utils.unregister_class(cls)
