import json
import logging
import re

import bpy
from .CR2W.witcher_cache.Bundles import LoadBundleManager
from .w3_material import (
    ensure_node_group,
    get_active_witcher_group_node,
    get_recommended_node_group_for_base_path,
    init_material_nodes,
)
from .w3_material_base_path import (
    create_base_material_helper,
    inspect_material_base_path,
    refresh_base_material_entry_state,
)
from .w3_material_reader import normalize_depot_path
from .w3_vector_param import (
    get_legacy_w_value,
    get_mapping_vector_input,
    get_vector_node_values,
    is_vector_param_node,
    mark_vector_param_node,
)
from . import get_all_addon_prefs, get_texture_path, get_uncook_path
from .extension_paths import get_cache_root
import os
from pathlib import Path
from types import SimpleNamespace

log = logging.getLogger(__name__)

_BASE_PATH_CACHE_FILE = Path(get_cache_root(create=True)) / "material_base_paths.json"
_BASE_PATH_ENUM_CACHE = {}
_NUMERIC_SUFFIX_RE = re.compile(r"\.\d{3}$")


def _cache_base_path_enum_items(cache_key, items):
    stable_items = []
    for item in items or [("", "No base materials found", "")]:
        identifier = str(item[0] or "")
        label = str(item[1] or identifier or "No base materials found")
        description = str(item[2] or "")
        stable_items.append((identifier, label, description))
    _BASE_PATH_ENUM_CACHE[cache_key] = stable_items
    return stable_items


def _load_material_base_path_cache():
    try:
        if not _BASE_PATH_CACHE_FILE.exists():
            return []
        with open(_BASE_PATH_CACHE_FILE, 'r', encoding='utf-8') as handle:
            payload = json.load(handle)
        paths = payload.get("paths", [])
        if not isinstance(paths, list):
            return []
        return [str(path) for path in paths if str(path or "").strip()]
    except Exception:
        log.warning("Failed to load material base path cache from %s", _BASE_PATH_CACHE_FILE, exc_info=True)
        return []


def _save_material_base_path_cache(paths):
    try:
        _BASE_PATH_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_BASE_PATH_CACHE_FILE, 'w', encoding='utf-8') as handle:
            json.dump({"paths": list(paths)}, handle, indent=2)
    except Exception:
        log.warning("Failed to save material base path cache to %s", _BASE_PATH_CACHE_FILE, exc_info=True)


def _gather_material_base_paths_from_bundles():
    paths = set()

    def add_candidate(candidate):
        normalized = normalize_depot_path(str(candidate or ""))
        if normalized.endswith((".w2mi", ".w2mg")):
            paths.add(normalized)

    bundle_manager = LoadBundleManager()
    items = getattr(bundle_manager, "Items", None) or {}
    for key, item_list in items.items():
        add_candidate(key)
        if not item_list:
            continue
        final_item = item_list[-1] if isinstance(item_list, list) else item_list
        add_candidate(getattr(final_item, "name", getattr(final_item, "Name", "")))

    return sorted(paths, key=str.lower)


def _material_base_path_enum_items(force_refresh: bool = False):
    cache_key = "bundle_material_base_paths"
    if not force_refresh and cache_key in _BASE_PATH_ENUM_CACHE:
        return _BASE_PATH_ENUM_CACHE[cache_key]

    paths = [] if force_refresh else _load_material_base_path_cache()
    if not paths:
        try:
            paths = _gather_material_base_paths_from_bundles()
        except Exception:
            log.warning("Failed to gather base material paths from bundles", exc_info=True)
            paths = []
        if paths:
            _save_material_base_path_cache(paths)

    items = [
        (
            path,
            path,
            f"Bundle material path ({Path(path).name})",
        )
        for path in paths
    ]
    return _cache_base_path_enum_items(cache_key, items)


def _material_base_path_values(force_refresh: bool = False):
    return [
        identifier
        for identifier, _label, _description in _material_base_path_enum_items(force_refresh)
        if identifier
    ]


def _filtered_material_base_paths(query: str = "", *, file_type: str = "ALL", limit: int = 0):
    paths = _material_base_path_values()
    if file_type == "W2MI":
        paths = [path for path in paths if path.lower().endswith(".w2mi")]
    elif file_type == "W2MG":
        paths = [path for path in paths if path.lower().endswith(".w2mg")]

    if not query:
        total = len(paths)
        return (paths[:limit] if limit > 0 else paths), total

    normalized_query = normalize_depot_path(str(query or "")).lower()

    def sort_key(path: str):
        lower = path.lower()
        basename = Path(path).name.lower()
        return (
            normalized_query not in basename,
            not basename.startswith(normalized_query),
            normalized_query not in lower,
            lower,
        )

    filtered = [
        path for path in paths
        if normalized_query in path.lower() or normalized_query in Path(path).name.lower()
    ]
    filtered.sort(key=sort_key)
    total = len(filtered)
    return (filtered[:limit] if limit > 0 else filtered), total


def _node_group_family_name(node_tree) -> str:
    name = str(getattr(node_tree, "name", "") or "")
    if not name:
        return ""
    return _NUMERIC_SUFFIX_RE.sub("", name)


def _base_path_group_recommendation(material):
    props = getattr(material, "witcher_props", None)
    if material is None or props is None:
        return None

    base_path = normalize_depot_path(getattr(props, "base_custom", ""))
    if not base_path:
        return None

    recommendation = get_recommended_node_group_for_base_path(material, base_path)
    if not recommendation.get("node_group_name"):
        return None

    active_group = get_active_witcher_group_node(material)
    current_tree = getattr(active_group, "node_tree", None) if active_group else None
    current_tree_name = str(getattr(current_tree, "name", "") or "")
    current_family = _node_group_family_name(current_tree)

    result = dict(recommendation)
    result["has_active_group"] = bool(active_group)
    result["current_tree_name"] = current_tree_name
    result["current_group_name"] = current_family
    result["matches_current"] = bool(
        current_family and current_family == _node_group_family_name(SimpleNamespace(name=recommendation["node_group_name"]))
    )
    return result

class ReplacePrincipledBSDFOperator(bpy.types.Operator):
    """Replace the selected Principled BSDF with a custom node group and reconnect inputs"""
    bl_idname = "witcher.replace_principled_bsdf"
    bl_label = "Replace Principled BSDF"

    def execute(self, context):
        # Get the current material and node tree
        material = context.material
        if not material:
            self.report({'ERROR'}, "No material selected")
            return {'CANCELLED'}

        node_tree = material.node_tree
        active_node = context.active_node
        if not active_node or active_node.type != 'BSDF_PRINCIPLED':
            self.report({'ERROR'}, "Please select a Principled BSDF node")
            return {'CANCELLED'}

        # Find the Material Output node
        output_node = next((n for n in node_tree.nodes if n.type == 'OUTPUT_MATERIAL' and n.is_active_output), None)
        if not output_node:
            self.report({'ERROR'}, "No active Material Output node found")
            return {'CANCELLED'}

        surface_input = output_node.inputs.get('Surface')
        if not (surface_input and surface_input.is_linked and surface_input.links[0].from_node == active_node):
            self.report({'ERROR'}, "Selected Principled BSDF is not connected to Material Output")
            return {'CANCELLED'}

        # Step 1: Store connections from Principled BSDF inputs
        base_color_input = active_node.inputs.get("Base Color")
        base_color_from_socket = base_color_input.links[0].from_socket if base_color_input and base_color_input.is_linked else None

        roughness_input = active_node.inputs.get("Roughness")
        roughness_from_socket = roughness_input.links[0].from_socket if roughness_input and roughness_input.is_linked else None

        normal_input = active_node.inputs.get("Normal")
        normal_from_socket = None
        if normal_input and normal_input.is_linked:
            normal_link = normal_input.links[0]
            normal_from_node = normal_link.from_node
            if normal_from_node.type == 'NORMAL_MAP':
                # If connected to a Normal Map, get the texture from its "Color" input
                color_input = normal_from_node.inputs.get("Color")
                if color_input and color_input.is_linked:
                    normal_from_socket = color_input.links[0].from_socket
            else:
                # Otherwise, use the direct connection
                normal_from_socket = normal_link.from_socket

        # Step 2: Store location and remove the Principled BSDF node
        node_location = active_node.location.copy()
        node_tree.nodes.remove(active_node)

        # Step 3: Add the new node group
        nodegroup = init_material_nodes(material, "Witcher3_Main", clear=False)
        if not nodegroup:
            self.report({'ERROR'}, "Failed to create node group")
            return {'CANCELLED'}
        nodegroup.location = node_location

        # Step 4: Connect the node group’s output to Material Output
        if nodegroup.outputs:
            node_tree.links.new(nodegroup.outputs[0], surface_input)
        else:
            self.report({'ERROR'}, "Node group has no outputs")
            return {'CANCELLED'}

        # Step 5: Reconnect the stored inputs to the node group
        if base_color_from_socket and "Diffuse" in nodegroup.inputs:
            node_tree.links.new(base_color_from_socket, nodegroup.inputs["Diffuse"])
        if roughness_from_socket and "Roughness" in nodegroup.inputs:
            node_tree.links.new(roughness_from_socket, nodegroup.inputs["Roughness"])
        if normal_from_socket and "Normal" in nodegroup.inputs:
            node_tree.links.new(normal_from_socket, nodegroup.inputs["Normal"])

        # Optional: Set the node group’s name based on the material
        nodegroup.name = material.name[-60:]

        self.report({'INFO'}, "Principled BSDF replaced successfully")
        return {'FINISHED'}


def _base_read_is_stale(mat_props) -> bool:
    return normalize_depot_path(getattr(mat_props, "base_read_requested_path", "")) != normalize_depot_path(getattr(mat_props, "base_custom", ""))


def _short_path_label(path: str, max_len: int = 64) -> str:
    text = str(path or "")
    if len(text) <= max_len:
        return text
    return "..." + text[-(max_len - 3):]


def _source_kind_label(source_kind: str) -> str:
    labels = {
        "instance": "Instance",
        "graph_default": "Graph Default",
        "declared_only": "Declared Only",
    }
    return labels.get(str(source_kind or ""), str(source_kind or "") or "Unknown")


def _compact_param_type_label(param_type: str) -> str:
    text = str(param_type or "")
    if text.startswith("handle:"):
        return text.split(":", 1)[1]
    return text


def _source_file_label(source_path: str) -> str:
    normalized = normalize_depot_path(source_path)
    if not normalized:
        return ""
    name = normalized.rsplit("\\", 1)[-1]
    return _short_path_label(name, 32)


def _status_icon(item) -> str:
    status = str(getattr(item, "status", "") or "")
    if status == "present_linked":
        return 'CHECKMARK'
    if status == "available_to_create":
        return 'ADD'
    if status == "unsupported_export_only":
        return 'LINKED'
    return 'INFO'


def _status_label(item) -> str:
    status = str(getattr(item, "status", "") or "")
    if status == "present_linked":
        return "Linked"
    if status == "available_to_create":
        return "Create"
    if status == "unsupported_export_only":
        return "Export Only"
    if status == "ignored_info":
        return "Ignored"
    if status == "declared_only_info":
        return "Declared Only"
    return "Info"


def _item_to_dict(item) -> dict:
    return {
        "name": str(getattr(item, "name", "") or ""),
        "param_type": str(getattr(item, "param_type", "") or ""),
        "value": str(getattr(item, "value", "") or ""),
        "source_kind": str(getattr(item, "source_kind", "") or ""),
        "source_path": str(getattr(item, "source_path", "") or ""),
        "has_value": bool(getattr(item, "has_value", False)),
        "has_matching_socket": bool(getattr(item, "has_matching_socket", False)),
        "is_linked": bool(getattr(item, "is_linked", False)),
        "is_supported": bool(getattr(item, "is_supported", False)),
        "is_declared_only": bool(getattr(item, "is_declared_only", False)),
        "can_create": bool(getattr(item, "can_create", False)),
        "status": str(getattr(item, "status", "") or ""),
        "message": str(getattr(item, "message", "") or ""),
    }


def _chain_text_from_inspection(inspection: dict) -> str:
    lines = []
    for entry in inspection.get("chain", []) or []:
        source_kind = _source_kind_label(entry.get("source_kind", ""))
        lines.append(f"{source_kind}: {entry.get('path', '')}")
    return "\n".join(lines)


def _set_base_read_snapshot(material, inspection: dict, *, status: str = "ok", message: str = "", count_created: int = 0):
    props = material.witcher_props
    props.base_read_status = status
    props.base_read_message = str(message or "")
    props.base_read_requested_path = str(inspection.get("requested_path", "") or "")
    props.base_read_resolved_graph = str(inspection.get("resolved_graph", "") or "")
    props.base_read_chain_text = _chain_text_from_inspection(inspection)
    props.base_read_count_created = int(count_created)

    counts = inspection.get("counts", {}) or {}
    props.base_read_count_present = int(counts.get("present", 0) or 0)
    props.base_read_count_unsupported = int(counts.get("unsupported", 0) or 0)
    props.base_read_count_declared_only = int(counts.get("declared_only", 0) or 0)

    props.base_read_params.clear()
    for entry in inspection.get("inventory", []) or []:
        item = props.base_read_params.add()
        item.name = str(entry.get("name", "") or "")
        item.param_type = str(entry.get("param_type", "") or "")
        item.value = str(entry.get("value", "") or "")
        item.source_kind = str(entry.get("source_kind", "") or "")
        item.source_path = str(entry.get("source_path", "") or "")
        item.has_value = bool(entry.get("has_value", False))
        item.has_matching_socket = bool(entry.get("has_matching_socket", False))
        item.is_linked = bool(entry.get("is_linked", False))
        item.is_supported = bool(entry.get("is_supported", False))
        item.is_declared_only = bool(entry.get("is_declared_only", False))
        item.can_create = bool(entry.get("can_create", False))
        item.status = str(entry.get("status", "") or "")
        item.message = str(entry.get("message", "") or "")

    props.base_read_show_inspector = bool(props.base_read_params)


def _sync_base_read_snapshot_state(material) -> None:
    if material is None or getattr(material, "witcher_props", None) is None:
        return
    props = material.witcher_props
    if not props.base_read_status or not props.base_read_requested_path:
        return

    present_count = 0
    unsupported_count = 0
    declared_count = 0
    for item in props.base_read_params:
        refreshed = refresh_base_material_entry_state(material, _item_to_dict(item))
        item.has_matching_socket = bool(refreshed.get("has_matching_socket", False))
        item.is_linked = bool(refreshed.get("is_linked", False))
        item.is_supported = bool(refreshed.get("is_supported", False))
        item.is_declared_only = bool(refreshed.get("is_declared_only", False))
        item.can_create = bool(refreshed.get("can_create", False))
        item.status = str(refreshed.get("status", "") or "")
        item.message = str(refreshed.get("message", "") or "")
        if item.status == "present_linked":
            present_count += 1
        if item.status == "unsupported_export_only":
            unsupported_count += 1
        if item.is_declared_only:
            declared_count += 1

    props.base_read_count_present = present_count
    props.base_read_count_unsupported = unsupported_count
    props.base_read_count_declared_only = declared_count


def _get_live_base_read_snapshot_state(material):
    if material is None or getattr(material, "witcher_props", None) is None:
        return [], {"present": 0, "unsupported": 0, "declared_only": 0}

    props = material.witcher_props
    if not props.base_read_status or not props.base_read_requested_path:
        items = [SimpleNamespace(**_item_to_dict(item)) for item in props.base_read_params]
        return items, {
            "present": int(getattr(props, "base_read_count_present", 0) or 0),
            "unsupported": int(getattr(props, "base_read_count_unsupported", 0) or 0),
            "declared_only": int(getattr(props, "base_read_count_declared_only", 0) or 0),
        }

    present_count = 0
    unsupported_count = 0
    declared_count = 0
    items = []
    for item in props.base_read_params:
        merged = _item_to_dict(item)
        refreshed = refresh_base_material_entry_state(material, merged)
        merged["has_matching_socket"] = bool(refreshed.get("has_matching_socket", False))
        merged["is_linked"] = bool(refreshed.get("is_linked", False))
        merged["is_supported"] = bool(refreshed.get("is_supported", False))
        merged["is_declared_only"] = bool(refreshed.get("is_declared_only", False))
        merged["can_create"] = bool(refreshed.get("can_create", False))
        merged["status"] = str(refreshed.get("status", "") or "")
        merged["message"] = str(refreshed.get("message", "") or "")
        if merged["status"] == "present_linked":
            present_count += 1
        if merged["status"] == "unsupported_export_only":
            unsupported_count += 1
        if merged["is_declared_only"]:
            declared_count += 1
        items.append(SimpleNamespace(**merged))

    return items, {
        "present": present_count,
        "unsupported": unsupported_count,
        "declared_only": declared_count,
    }


def _find_base_read_param_item(mat_props, param_name: str):
    for item in mat_props.base_read_params:
        if item.name == param_name:
            return item
    return None


def _apply_base_read_entries(context, material, entries, *, allow_export_socket: bool = False):
    if not entries:
        return 0, 0

    node_ng = get_active_witcher_group_node(material)
    created = 0
    reused = 0
    uncook_path = get_texture_path(context)
    for entry in entries:
        node_ng, node, action = create_base_material_helper(
            material,
            entry,
            uncook_path,
            node_ng=node_ng,
            allow_export_socket=allow_export_socket,
        )
        if action == "created":
            created += 1
        elif action == "reused":
            reused += 1
    return created, reused

class WITCH_PT_materials(bpy.types.Panel):
    bl_label = "Witcher"
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = "Witcher"

    def _draw_base_path_controls(self, layout, mat):
        props = mat.witcher_props
        row = layout.row(align=True)
        row.prop(props, "base_custom", text="Base Path")
        row.operator("witcher.search_base_material_path", text="", icon='VIEWZOOM')
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
        for stored_item, item in items:
            row = layout.row(align=True)
            row.prop(
                stored_item,
                "show_details",
                icon="TRIA_DOWN" if stored_item.show_details else "TRIA_RIGHT",
                icon_only=True,
                emboss=False,
            )
            row.label(text=item.name, icon=_status_icon(item))
            if item.param_type:
                row.label(text=_compact_param_type_label(item.param_type))
            source_file = _source_file_label(getattr(item, "source_path", ""))
            if source_file:
                row.label(text=source_file)

            if action_enabled and item.can_create and not item.is_linked:
                op = row.operator(
                    "witcher.create_base_material_param",
                    text=_status_label(item),
                    icon='ADD' if item.has_matching_socket else 'LINKED',
                )
                op.param_name = item.name
                op.create_export_socket = not item.has_matching_socket
            else:
                status = str(getattr(item, "status", "") or "")
                status_text = _status_label(item)
                if status != "present_linked" and status_text:
                    row.label(text=status_text)

            if not stored_item.show_details:
                continue

            details = layout.column(align=True)
            details.scale_y = 0.9
            if item.value:
                details.label(text=f"Value: {item.value}")
            if item.source_kind:
                details.label(text=f"Source: {_source_kind_label(item.source_kind)}")
            if item.source_path:
                details.label(text=f"Path: {item.source_path}")
            if item.message:
                details.label(text=item.message, icon='INFO')

    def _draw_base_read_section(self, layout, context, mat):
        props = mat.witcher_props
        if not props.base_read_status:
            empty_row = layout.row()
            empty_row.label(text="Base Path not loaded.", icon='INFO')
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
                if item.status == "available_to_create" and item.can_create
            )
            counts_text = (
                f"Linked {live_counts['present']}"
                f" | Available {available_count}"
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
                alert_text = props.base_read_message or "Base Path read failed."
            elif stale:
                header_row.label(text="Snapshot is stale", icon='ERROR')
                alert_text = "Base Path changed; read again."
            else:
                header_row.label(text="Snapshot loaded", icon='CHECKMARK')
            header_row.label(text="Base Path Snapshot")
            header_row.label(text=counts_text)

            action_row = header_row.row(align=True)
            action_row.enabled = (
                props.base_read_status == "ok"
                and not stale
                and material_ready
                and any(
                    item.status == "available_to_create" and item.can_create
                    for _, item in items
                )
            )
            action_row.operator("witcher.create_missing_base_material_params", text="Create Missing Supported", icon='ADD')

            if not props.base_read_show_inspector:
                return

            if alert_text:
                alert_row = snapshot_box.row()
                alert_row.alert = True
                alert_row.label(text=alert_text, icon='ERROR')

            if props.base_read_chain_text:
                chain_col = snapshot_box.column(align=True)
                chain_col.scale_y = 0.9
                chain_col.label(text="Material Chain", icon='LINKED')
                for line in props.base_read_chain_text.splitlines():
                    chain_col.label(text=_short_path_label(line, 100))

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
            self._draw_base_read_items(snapshot_box, mat, items, action_enabled=action_enabled)
        except Exception:
            log.exception("Failed to draw Base Path UI for material '%s'", getattr(mat, "name", "<unknown>"))
            error_row = layout.row()
            error_row.label(text="Base Path UI error. See console for details.", icon='ERROR')

    def _draw_material_socket_controls(self, layout, mat):
        group_inputs = get_group_inputs(mat)
        if not group_inputs:
            layout.label(text="No active Witcher shader group inputs found.", icon='INFO')
            return

        linked_count = 0
        for input_socket in group_inputs:
            if input_socket.is_linked:
                linked_count += 1
                linked_socket = input_socket.links[0].from_socket

                row = layout.row()
                row.prop(linked_socket.node, "witcher_include", text=input_socket.name + ":")

                if linked_socket.node.type == 'TEX_IMAGE':
                    row.prop(linked_socket.node, "image", text="")
                    if linked_socket.node.image is not None:
                        rel_path = win_unprefix_path(linked_socket.node.image.filepath)
                        abs_path = win_unprefix_path(bpy.path.abspath(rel_path))
                        texture_path = os.path.normpath(abs_path)
                        final_path = get_repo_from_abs_path(texture_path)
                        if mat.witcher_props.override_texture_root:
                            display_path = mat.witcher_props.custom_texture_root + os.path.basename(final_path)
                        else:
                            display_path = final_path
                        resolved = is_path_resolved(display_path)
                        icon = 'CHECKMARK' if resolved else 'ERROR'
                        path_row = layout.row(align=True)
                        path_row.label(text="", icon=icon)
                        path_row.label(text=display_path)
                        op = path_row.operator("witcher.copy_texture_path", text="", icon='COPYDOWN')
                        op.path = display_path
                elif linked_socket.node.type == 'RGB':
                    row.prop(linked_socket, "default_value", text="")
                elif linked_socket.node.type == 'VALUE':
                    row.prop(linked_socket, "default_value", text="")
                elif input_socket.type == 'VECTOR':
                    vector_node = linked_socket.node
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

        if linked_count == 0:
            layout.label(text="No linked local params on the active Witcher shader group.", icon='INFO')

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


class NodeGroupInputProperties(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty(name="Name")
    value: bpy.props.StringProperty(name="Value")
    value_float: bpy.props.FloatProperty(name="Value")
    value_vector:bpy.props.FloatVectorProperty(name="Value")
    #type: bpy.props.EnumProperty(name="Type", items=[("FLOAT", "Float", ""), ("VECTOR", "Vector", ""), ("COLOR", "Color", "")])
    type: bpy.props.StringProperty(name="Type")
    is_enabled: bpy.props.BoolProperty(name="Is Enabled", default=False)
    is_enabled_temp: bpy.props.BoolProperty(name="Export", default=False)
    is_linked: bpy.props.BoolProperty(name="is_linked", default=False)


class BaseMaterialPathItem(bpy.types.PropertyGroup):
    path: bpy.props.StringProperty(name="Path")


class WITCH_UL_base_material_paths(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        layout.label(text=getattr(item, "path", "") or "", icon='FILE')


class BaseMaterialParamItem(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty(name="Name")
    param_type: bpy.props.StringProperty(name="Type")
    value: bpy.props.StringProperty(name="Value")
    source_kind: bpy.props.StringProperty(name="Source Kind")
    source_path: bpy.props.StringProperty(name="Source Path")
    has_value: bpy.props.BoolProperty(name="Has Value", default=False)
    has_matching_socket: bpy.props.BoolProperty(name="Has Matching Socket", default=False)
    is_linked: bpy.props.BoolProperty(name="Is Linked", default=False)
    is_supported: bpy.props.BoolProperty(name="Is Supported", default=False)
    is_declared_only: bpy.props.BoolProperty(name="Is Declared Only", default=False)
    can_create: bpy.props.BoolProperty(name="Can Create", default=False)
    status: bpy.props.StringProperty(name="Status")
    message: bpy.props.StringProperty(name="Message")
    show_details: bpy.props.BoolProperty(name="Show Details", default=False)

class WitcherMaterialProperties(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty(name="name", default="Material")
    enableMask: bpy.props.BoolProperty(name="enableMask", default=False, description="Enable Mask of hair etc")
    local: bpy.props.BoolProperty(name="local", default=True, description="Local materials will be embedded in the .w2mesh. Non-local will use the defined base material without any instances.")
    material_ui_tab: bpy.props.EnumProperty(
        name="Material UI Tab",
        items=[
            ('EXPORT', "Export Params", "Export-connected local params"),
            ('BASE', "Base", "Read base path and create params from it"),
        ],
        default='EXPORT',
    )
    #base: bpy.props.StringProperty(name="base", default="engine\materials\graphs\pbr_std.w2mg")
    bind_name: bpy.props.BoolProperty(name="Use Blender Material Name", default=True)
    node_group_name: bpy.props.StringProperty(name="Node Group", default="")
    input_props: bpy.props.CollectionProperty(type=NodeGroupInputProperties)
    input_props_index: bpy.props.IntProperty()
    xml_text : bpy.props.StringProperty(name="XML Text")
    witcher_material_settings_collapse: bpy.props.BoolProperty(default = False)
    override_texture_root: bpy.props.BoolProperty(name="override_texture_root", default=False, description="Specify a root path")
    custom_texture_root: bpy.props.StringProperty(name="custom_texture_root", default="", description="Root path of textures for this material")
    base_read_status: bpy.props.StringProperty(name="Base Read Status", default="")
    base_read_message: bpy.props.StringProperty(name="Base Read Message", default="")
    base_read_requested_path: bpy.props.StringProperty(name="Base Read Requested Path", default="")
    base_read_resolved_graph: bpy.props.StringProperty(name="Base Read Resolved Graph", default="")
    base_read_chain_text: bpy.props.StringProperty(name="Base Read Chain", default="")
    base_read_params: bpy.props.CollectionProperty(type=BaseMaterialParamItem)
    base_read_count_created: bpy.props.IntProperty(name="Base Read Created", default=0)
    base_read_count_present: bpy.props.IntProperty(name="Base Read Present", default=0)
    base_read_count_unsupported: bpy.props.IntProperty(name="Base Read Unsupported", default=0)
    base_read_count_declared_only: bpy.props.IntProperty(name="Base Read Declared Only", default=0)
    base_read_show_inspector: bpy.props.BoolProperty(name="Show Base Read Inspector", default=True)
    base_read_show_info: bpy.props.BoolProperty(name="Show Base Read Info", default=False)
    base_read_present_collapse: bpy.props.BoolProperty(name="Show Present Linked", default=False)
    base_read_available_collapse: bpy.props.BoolProperty(name="Show Available Defaults", default=False)
    base_read_declared_collapse: bpy.props.BoolProperty(name="Show Declared Unsupported", default=False)



    # base_options = [
    #     ("custom", "Custom", "Description for value 1"),
    #     (r"engine\materials\graphs\pbr_std.w2mg", r"engine\materials\graphs\pbr_std.w2mg" , ""),
    #     (r"engine\materials\graphs\pbr_std_colorshift.w2mg", r"engine\materials\graphs\pbr_std_colorshift.w2mg" , ""),
    #     (r"engine\materials\graphs\pbr_std_tint_mask_2det.w2mg", r"engine\materials\graphs\pbr_std_tint_mask_2det.w2mg" , ""),
    #     (r"engine\materials\graphs\pbr_std_tint_mask_2det_fresnel.w2mg", r"engine\materials\graphs\pbr_std_tint_mask_2det_fresnel.w2mg" , ""),
    #     (r"engine\materials\graphs\pbr_std_tint_mask_det.w2mg", r"engine\materials\graphs\pbr_std_tint_mask_det.w2mg" , ""),
    #     (r"engine\materials\graphs\pbr_std_tint_mask_det_fresnel.w2mg", r"engine\materials\graphs\pbr_std_tint_mask_det_fresnel.w2mg" , ""),
    #     (r"engine\materials\graphs\pbr_std_tint_mask_det_pattern.w2mg", r"engine\materials\graphs\pbr_std_tint_mask_det_pattern.w2mg" , ""),
    #     (r"engine\materials\diffusecubemap.w2mg", r"engine\materials\diffusecubemap.w2mg" , ""),
    #     (r"engine\materials\diffusemap.w2mg", r"engine\materials\diffusemap.w2mg" , ""),
    #     (r"engine\materials\gridmat.w2mg", r"engine\materials\gridmat.w2mg" , ""),
    #     (r"engine\materials\lens_flare.w2mg", r"engine\materials\lens_flare.w2mg" , ""),
    #     (r"engine\materials\normalmap.w2mg", r"engine\materials\normalmap.w2mg" , ""),
    #     (r"engine\materials\defaults\apex.w2mg", r"engine\materials\defaults\apex.w2mg" , ""),
    #     (r"engine\materials\defaults\flare.w2mg", r"engine\materials\defaults\flare.w2mg" , ""),
    #     (r"engine\materials\defaults\mergedmesh.w2mg", r"engine\materials\defaults\mergedmesh.w2mg" , ""),
    #     (r"engine\materials\defaults\mesh.w2mg", r"engine\materials\defaults\mesh.w2mg" , ""),
    #     (r"engine\materials\defaults\volume.w2mg", r"engine\materials\defaults\volume.w2mg" , ""),
    #     (r"engine\materials\editor\terrain_selector.w2mg", r"engine\materials\editor\terrain_selector.w2mg" , ""),
    #     (r"engine\materials\graphs\character_dismemberment_fx.w2mg", r"engine\materials\graphs\character_dismemberment_fx.w2mg" , ""),
    #     (r"engine\materials\graphs\debug.w2mg", r"engine\materials\graphs\debug.w2mg" , ""),
    #     (r"engine\materials\graphs\pbr_det.w2mg", r"engine\materials\graphs\pbr_det.w2mg" , ""),
    #     (r"engine\materials\graphs\pbr_eye.w2mg", r"engine\materials\graphs\pbr_eye.w2mg" , ""),
    #     (r"engine\materials\graphs\pbr_hair.w2mg", r"engine\materials\graphs\pbr_hair.w2mg" , ""),
    #     (r"engine\materials\graphs\pbr_hair_moving.w2mg", r"engine\materials\graphs\pbr_hair_moving.w2mg" , ""),
    #     (r"engine\materials\graphs\pbr_hair_simple.w2mg", r"engine\materials\graphs\pbr_hair_simple.w2mg" , ""),
    #     (r"engine\materials\graphs\pbr_simple.w2mg", r"engine\materials\graphs\pbr_simple.w2mg" , ""),
    #     (r"engine\materials\graphs\pbr_simple_no_emmisive.w2mg", r"engine\materials\graphs\pbr_simple_no_emmisive.w2mg" , ""),
    #     (r"engine\materials\graphs\pbr_skin.w2mg", r"engine\materials\graphs\pbr_skin.w2mg" , ""),
    #     (r"engine\materials\graphs\pbr_skin_decal.w2mg", r"engine\materials\graphs\pbr_skin_decal.w2mg" , ""),
    #     (r"engine\materials\graphs\pbr_skin_simple.w2mg", r"engine\materials\graphs\pbr_skin_simple.w2mg" , ""),
    #     (r"engine\materials\graphs\pbr_skin_simple_under.w2mg", r"engine\materials\graphs\pbr_skin_simple_under.w2mg" , ""),
    #     (r"engine\materials\graphs\pbr_spec.w2mg", r"engine\materials\graphs\pbr_spec.w2mg" , ""),
    #     (r"engine\materials\graphs\pbr_spec_tint_mask_det.w2mg", r"engine\materials\graphs\pbr_spec_tint_mask_det.w2mg" , ""),
    #     (r"engine\materials\graphs\pbr_swarm.w2mg", r"engine\materials\graphs\pbr_swarm.w2mg" , ""),
    #     (r"engine\materials\graphs\pbr_vert_blend.w2mg", r"engine\materials\graphs\pbr_vert_blend.w2mg" , ""),
    #     (r"engine\materials\graphs\transparent_lit.w2mg", r"engine\materials\graphs\transparent_lit.w2mg" , ""),
    #     (r"engine\materials\graphs\transparent_lit_vert.w2mg", r"engine\materials\graphs\transparent_lit_vert.w2mg" , ""),
    #     (r"engine\materials\graphs\transparent_reflective.w2mg", r"engine\materials\graphs\transparent_reflective.w2mg" , ""),
    #     (r"engine\materials\graphs\eyeshadow\pbr_eye_shadow.w2mg", r"engine\materials\graphs\eyeshadow\pbr_eye_shadow.w2mg" , ""),
    #     (r"engine\materials\graphs\morphblend\pbr_skin_morph.w2mg", r"engine\materials\graphs\morphblend\pbr_skin_morph.w2mg" , ""),
    #     (r"engine\materials\graphs\morphblend\pbr_std_morph.w2mg", r"engine\materials\graphs\morphblend\pbr_std_morph.w2mg" , ""),
    #     (r"engine\materials\graphs\normalblend\pbr_skin_normalblend.w2mg", r"engine\materials\graphs\normalblend\pbr_skin_normalblend.w2mg" , ""),
    #     (r"engine\materials\render\billboard.w2mg", r"engine\materials\render\billboard.w2mg" , ""),
    #     (r"engine\materials\render\fallback.w2mg", r"engine\materials\render\fallback.w2mg" , "")
    # ]
    # base: bpy.props.EnumProperty(
    #     name="Base",
    #     description="Select a value from the dropdown or enter a custom value",
    #     items=base_options,
    #     default=r"engine\materials\graphs\pbr_std.w2mg",
    # )
    base_custom: bpy.props.StringProperty(
        name="Base Path",
        description="Enter a .w2mi or .w2mg path",
        default=r"engine\materials\graphs\pbr_std.w2mg",
    )
    
    
    material_version_options = [
        #("custom", "Custom", "Description for value 1"),
        ("witcher3", "Witcher 3", "This is a Witcher 3 material"),
        ("witcher2", "Witcher 2", "This is a Witcher 2 material"),
    ]
    material_version: bpy.props.EnumProperty(
        name="Game",
        description="What game this material was orignally for",
        items=material_version_options,
        default="witcher3",
    )
    
def get_group_inputs(mat):
    if mat and mat.witcher_props and mat.node_tree and mat.node_tree.nodes:
        node = get_active_witcher_group_node(mat)
        if node is None:
            return None
        input_names = {
            str(getattr(input_socket, "name", "") or "")
            for input_socket in node.inputs
        }
        return [
            input_socket for input_socket in node.inputs
            if not (
                str(getattr(input_socket, "name", "") or "").endswith("_W")
                and str(getattr(input_socket, "name", "") or "")[:-2] in input_names
            )
        ]
    return None

from .CR2W.common_blender import win_unprefix_path


possible_folders = [
    'files\\Raw\\Mod',
    'files\\Raw\\DLC',
    'files\\Mod\\Cooked',
    'files\\Mod\\Uncooked',
    'files\\DLC\\Cooked',
    'files\\DLC\\Uncooked',
]

from . import get_mod_directory, get_modded_texture_path
# def get_repo_from_abs_path(texture_path_input):
#     texture_path = os.path.realpath(bpy.path.abspath(texture_path_input))
#     TEXTURE_PATH = get_texture_path(bpy.context)
#     MOD_DIR = get_mod_directory(bpy.context)
#     MOD_TEX_PATH = get_modded_texture_path(bpy.context)
    
#     #path_obj = Path(texture_path)
#     TEXTURE_PATH_obj = Path(TEXTURE_PATH)
#     MOD_DIR_obj = Path(MOD_DIR)
#     MOD_TEX_PATH_obj = Path(MOD_TEX_PATH)
    
#     if TEXTURE_PATH_obj.exists() and TEXTURE_PATH in texture_path:
#         texture_path = texture_path.replace(TEXTURE_PATH+'\\', '')
#     elif MOD_DIR_obj.exists() and MOD_DIR in texture_path:
#         texture_path = texture_path.replace(MOD_DIR+'\\', '')
#         for folder in possible_folders:
#             if folder in texture_path:
#                 texture_path = texture_path.replace(folder+'\\', '')
#                 break
#     elif MOD_TEX_PATH_obj.exists() and MOD_TEX_PATH in texture_path:
#         texture_path = texture_path.replace(MOD_TEX_PATH+'\\', '')

#     return texture_path

def get_repo_from_abs_path(texture_path_input, extension='.xbm'):
    texture_path_input = win_unprefix_path(texture_path_input)
    texture_path = os.path.realpath(bpy.path.abspath(texture_path_input))
    texture_path = win_unprefix_path(texture_path)

    TEXTURE_PATH = get_texture_path(bpy.context)
    UNCOOK_PATH = get_uncook_path(bpy.context)
    MOD_DIR = get_mod_directory(bpy.context)
    MOD_TEX_PATH = get_modded_texture_path(bpy.context)

    addon_prefs = get_all_addon_prefs(bpy.context)

    # Ensure the path ends with the specified extension
    texture_path_no_ext = os.path.splitext(texture_path)[0]
    texture_path = texture_path_no_ext + extension

    def _try_strip_root(path, root):
        """Strip a root directory from the path, returning game-relative path or None."""
        root = win_unprefix_path(os.path.realpath(bpy.path.abspath(root)))
        if root and Path(root).exists() and root in path:
            return path.replace(root + '\\', '')
        return None

    # Check paths in path_list first (user custom roots)
    for path_item in addon_prefs.path_list:
        result = _try_strip_root(texture_path, path_item.path)
        if result:
            return result

    # REDkit project paths
    for path_item in addon_prefs.redkit_projects:
        if path_item.path:
            # Try workspace subfolder first (REDkit convention)
            result = _try_strip_root(texture_path, os.path.join(path_item.path, "workspace"))
            if not result:
                result = _try_strip_root(texture_path, path_item.path)
            if result:
                return result

    # REDkit uncooked depot
    result = _try_strip_root(texture_path, addon_prefs.redkit_uncooked_path)
    if result:
        return result

    # REDkit depot (r4data)
    result = _try_strip_root(texture_path, addon_prefs.redkit_depot_path)
    if result:
        return result

    # Texture uncook path
    result = _try_strip_root(texture_path, TEXTURE_PATH)
    if result:
        return result

    # Uncook path
    result = _try_strip_root(texture_path, UNCOOK_PATH)
    if result:
        return result

    # Mod directory
    if MOD_DIR and Path(MOD_DIR).exists() and MOD_DIR in texture_path:
        texture_path = texture_path.replace(MOD_DIR + '\\', '')
        for folder in possible_folders:
            if folder in texture_path:
                texture_path = texture_path.replace(folder + '\\', '')
                break
        return texture_path

    # Modded texture path
    result = _try_strip_root(texture_path, MOD_TEX_PATH)
    if result:
        return result

    game_repo_path = os.path.splitdrive(texture_path)[1]
    return game_repo_path.lstrip('\\/')


def is_path_resolved(path):
    """Check if a path is a game-relative (resolved) path vs an absolute path."""
    if not path:
        return True
    # Absolute paths have drive letters (C:\) or UNC paths (\\)
    return not os.path.isabs(path)



def get_socket_value(input_socket):
    if input_socket.is_linked:
        linked_socket = input_socket.links[0].from_socket
        if linked_socket.node.type == 'TEX_IMAGE' and linked_socket.node.image:
            mat = next((m for m in bpy.data.materials if m.node_tree == input_socket.node.id_data and hasattr(m, 'witcher_props')), None)
            rel_path = win_unprefix_path(linked_socket.node.image.filepath)
            abs_path = win_unprefix_path(bpy.path.abspath(rel_path))
            texture_path = os.path.normpath(abs_path)
            final_path = get_repo_from_abs_path(texture_path)
            if mat.witcher_props.override_texture_root:
                return mat.witcher_props.custom_texture_root + os.path.basename(final_path)
            else:
                return final_path
        elif linked_socket.node.type == 'RGB':
            color_value = linked_socket.node.outputs[0].default_value
            return " ; ".join(str(x) for x in color_value)
        elif linked_socket.node.type == 'VALUE':
            value = linked_socket.node.outputs[0].default_value
            return value
        elif linked_socket.type == 'VECTOR':
            vector_node = linked_socket.node
            if vector_node.type in {'COMBXYZ', 'MAPPING'}:
                if not getattr(vector_node, "witcher_param_kind", ""):
                    legacy_w = get_legacy_w_value(input_socket, None)
                    if legacy_w is not None:
                        mark_vector_param_node(vector_node, input_socket.name, legacy_w)
                value = get_vector_node_values(vector_node, input_socket.name, get_legacy_w_value(input_socket, 1.0))
                return value
            try:
                value = [float(input_socket.default_value[i]) for i in range(3)]
            except Exception:
                value = [0.0, 0.0, 0.0]
            value.append(float(get_legacy_w_value(input_socket, 1.0)))
            return value
    try:
        default_value = " ; ".join(str(x) for x in input_socket.default_value)
    except Exception as e:
        default_value = str(input_socket.default_value)
    return default_value


def _refresh_base_read_snapshot(material, material_path: str, *, count_created: int = 0, status: str = "ok", message: str = "") -> dict:
    inspection = inspect_material_base_path(material, material_path)
    if inspection.get("errors"):
        status = "error"
        if not message:
            message = str(inspection["errors"][0])
    _set_base_read_snapshot(material, inspection, status=status, message=message, count_created=count_created)
    return inspection


class WITCH_OT_search_base_material_path(bpy.types.Operator):
    bl_idname = "witcher.search_base_material_path"
    bl_label = "Search Base Path"
    bl_description = "Search bundle .w2mi and .w2mg paths to populate the Base Path"
    bl_options = {'REGISTER', 'INTERNAL'}

    filter_text: bpy.props.StringProperty(name="Search", default="")
    file_type: bpy.props.EnumProperty(
        name="Type",
        items=[
            ('ALL', "All", "Show both .w2mi and .w2mg"),
            ('W2MI', "w2mi", "Show only .w2mi"),
            ('W2MG', "w2mg", "Show only .w2mg"),
        ],
        default='ALL',
    )
    base_path_items: bpy.props.CollectionProperty(type=BaseMaterialPathItem)
    base_path_items_index: bpy.props.IntProperty(default=0)

    def _rebuild_items(self):
        matches, _total = _filtered_material_base_paths(self.filter_text, file_type=self.file_type)
        self.base_path_items.clear()
        for path in matches:
            item = self.base_path_items.add()
            item.path = path
        if self.base_path_items:
            self.base_path_items_index = min(max(int(self.base_path_items_index), 0), len(self.base_path_items) - 1)
        else:
            self.base_path_items_index = -1

    def invoke(self, context, event):
        material = context.material
        if material is None or getattr(material, "witcher_props", None) is None:
            self.report({'ERROR'}, "No material selected")
            return {'CANCELLED'}

        current = normalize_depot_path(getattr(material.witcher_props, "base_custom", ""))
        self.filter_text = ""
        self.file_type = 'ALL'

        if not _material_base_path_values():
            self.report({'WARNING'}, "No .w2mi or .w2mg bundle paths were found")
            return {'CANCELLED'}

        self._rebuild_items()
        if current and self.base_path_items:
            for idx, item in enumerate(self.base_path_items):
                if normalize_depot_path(getattr(item, "path", "")) == current:
                    self.base_path_items_index = idx
                    break

        return context.window_manager.invoke_props_dialog(self, width=980)

    def check(self, context):
        self._rebuild_items()
        return True

    def draw(self, context):
        layout = self.layout
        row = layout.row(align=True)
        row.prop(self, "filter_text", text="", icon='VIEWZOOM')
        type_row = layout.row(align=True)
        type_row.prop(self, "file_type", expand=True)

        total = len(self.base_path_items)
        if total == 0:
            layout.label(text="No matching .w2mi or .w2mg paths found.", icon='INFO')
            return

        list_box = layout.box()
        list_box.template_list(
            "WITCH_UL_base_material_paths",
            "",
            self,
            "base_path_items",
            self,
            "base_path_items_index",
            rows=18,
        )
        layout.label(text=f"{total} path(s)", icon='INFO')

    def execute(self, context):
        material = context.material
        if material is None or getattr(material, "witcher_props", None) is None:
            return {'CANCELLED'}
        if not (0 <= self.base_path_items_index < len(self.base_path_items)):
            return {'CANCELLED'}
        material.witcher_props.base_custom = self.base_path_items[self.base_path_items_index].path
        return {'FINISHED'}


class WITCH_OT_use_recommended_base_material_group(bpy.types.Operator):
    bl_idname = "witcher.use_recommended_base_material_group"
    bl_label = "Use Recommended Group"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        material = context.material
        if material is None or getattr(material, "witcher_props", None) is None:
            self.report({'ERROR'}, "No material selected")
            return {'CANCELLED'}

        node_ng = get_active_witcher_group_node(material)
        if node_ng is None:
            self.report({'ERROR'}, "No active Witcher shader group is connected to Material Output")
            return {'CANCELLED'}

        recommendation = _base_path_group_recommendation(material)
        if not recommendation:
            self.report({'ERROR'}, "Base Path does not resolve to a recommended node group")
            return {'CANCELLED'}

        recommended_name = str(recommendation.get("node_group_name", "") or "")
        if not recommended_name:
            self.report({'ERROR'}, "No recommended node group was found")
            return {'CANCELLED'}

        current_tree = getattr(node_ng, "node_tree", None)
        if _node_group_family_name(current_tree) == _node_group_family_name(SimpleNamespace(name=recommended_name)):
            self.report({'INFO'}, f"Active group already matches {recommended_name}")
            return {'CANCELLED'}

        ng = ensure_node_group(recommended_name, resource_path=recommendation.get("resource_path"))
        node_ng.node_tree = ng
        if recommendation.get("shader_type"):
            node_ng.label = recommendation["shader_type"]
        material.witcher_props.node_group_name = ng.name
        self.report({'INFO'}, f"Updated active group to {ng.name}")
        return {'FINISHED'}


class WITCH_OT_read_base_material(bpy.types.Operator):
    bl_idname = "witcher.read_base_material"
    bl_label = "Load"
    bl_options = {'REGISTER', 'UNDO'}

    def _inspection(self, context):
        inspection = getattr(self, "_cached_inspection", None)
        if inspection is None:
            inspection = inspect_material_base_path(context.material)
            self._cached_inspection = inspection
        return inspection

    def invoke(self, context, event):
        material = context.material
        if material is None or getattr(material, "witcher_props", None) is None:
            self.report({'ERROR'}, "No material selected")
            return {'CANCELLED'}

        inspection = inspect_material_base_path(material)
        self._cached_inspection = inspection
        if inspection.get("errors"):
            message = str(inspection["errors"][0])
            _set_base_read_snapshot(material, inspection, status="error", message=message, count_created=0)
            self.report({'ERROR'}, message)
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self, width=560)

    def draw(self, context):
        inspection = self._inspection(context)
        layout = self.layout
        counts = inspection.get("counts", {}) or {}

        layout.label(text="Read the current Base Path and fill only missing supported sockets.", icon='INFO')
        layout.label(text=f"Requested: {_short_path_label(inspection.get('requested_path', ''), 96)}")
        if inspection.get("resolved_graph"):
            layout.label(text=f"Resolved Graph: {_short_path_label(inspection.get('resolved_graph', ''), 96)}")

        chain_box = layout.box()
        chain_box.label(text="Inheritance Chain", icon='LINKED')
        for entry in inspection.get("chain", []) or []:
            chain_box.label(text=_short_path_label(f"{_source_kind_label(entry.get('source_kind', ''))}: {entry.get('path', '')}", 100))

        counts_box = layout.box()
        counts_box.label(text=f"Concrete Params: {counts.get('concrete', 0)}")
        counts_box.label(text=f"Declared Only: {counts.get('declared_only', 0)}")
        counts_box.label(text=f"Missing Supported Sockets: {counts.get('available', 0)}")
        counts_box.label(text=f"Already Linked: {counts.get('present', 0)}")
        counts_box.label(text=f"Unsupported / Export Only: {counts.get('unsupported', 0)}")

        note_box = layout.box()
        note_box.label(text="Any existing nodes are preserved.", icon='CHECKMARK')
        if not inspection.get("has_active_witcher_group"):
            note_box.label(text="No active Witcher shader group is connected; this read will only load the snapshot.", icon='INFO')

    def execute(self, context):
        material = context.material
        inspection = self._inspection(context)
        if material is None:
            self.report({'ERROR'}, "No material selected")
            return {'CANCELLED'}
        if inspection.get("errors"):
            message = str(inspection["errors"][0])
            _set_base_read_snapshot(material, inspection, status="error", message=message, count_created=0)
            self.report({'ERROR'}, message)
            return {'CANCELLED'}

        entries = [
            entry for entry in inspection.get("inventory", []) or []
            if entry.get("status") == "available_to_create" and entry.get("has_matching_socket") and entry.get("can_create")
        ]
        created, reused = _apply_base_read_entries(context, material, entries, allow_export_socket=False)

        message = f"Loaded Base Path snapshot. Created {created} helper node(s)"
        if reused:
            message += f", reused {reused}"
        if not inspection.get("has_active_witcher_group"):
            message += ". No active Witcher shader group was connected, so no nodes were created."
        post = _refresh_base_read_snapshot(
            material,
            inspection.get("requested_path", ""),
            count_created=created,
            status="ok",
            message=message,
        )
        if post.get("warnings"):
            message = f"{message}. {post['warnings'][0]}"
        self.report({'INFO'}, message)
        return {'FINISHED'}


class WITCH_OT_create_missing_base_material_params(bpy.types.Operator):
    bl_idname = "witcher.create_missing_base_material_params"
    bl_label = "Create Missing Supported Base Material Params"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        material = context.material
        if material is None or getattr(material, "witcher_props", None) is None:
            self.report({'ERROR'}, "No material selected")
            return {'CANCELLED'}

        props = material.witcher_props
        if props.base_read_status != "ok" or not props.base_read_requested_path:
            self.report({'WARNING'}, "Read the Base Path first.")
            return {'CANCELLED'}
        _sync_base_read_snapshot_state(material)
        if _base_read_is_stale(props):
            self.report({'WARNING'}, "The loaded Base Path snapshot is stale. Read the current Base Path again.")
            return {'CANCELLED'}

        entries = [
            _item_to_dict(item) for item in props.base_read_params
            if item.status == "available_to_create" and item.can_create and item.has_matching_socket and not item.is_linked
        ]
        created, reused = _apply_base_read_entries(context, material, entries, allow_export_socket=False)
        message = f"Created {created} missing helper node(s)"
        if reused:
            message += f", reused {reused}"
        _refresh_base_read_snapshot(
            material,
            props.base_read_requested_path,
            count_created=created,
            status="ok",
            message=message,
        )
        self.report({'INFO'}, message)
        return {'FINISHED'}


class WITCH_OT_create_base_material_param(bpy.types.Operator):
    bl_idname = "witcher.create_base_material_param"
    bl_label = "Create Base Material Param"
    bl_options = {'REGISTER', 'UNDO'}

    param_name: bpy.props.StringProperty()
    create_export_socket: bpy.props.BoolProperty(default=False)

    def execute(self, context):
        material = context.material
        if material is None or getattr(material, "witcher_props", None) is None:
            self.report({'ERROR'}, "No material selected")
            return {'CANCELLED'}

        props = material.witcher_props
        if props.base_read_status != "ok" or not props.base_read_requested_path:
            self.report({'WARNING'}, "Read the Base Path first.")
            return {'CANCELLED'}
        _sync_base_read_snapshot_state(material)
        if _base_read_is_stale(props):
            self.report({'WARNING'}, "The loaded Base Path snapshot is stale. Read the current Base Path again.")
            return {'CANCELLED'}

        item = _find_base_read_param_item(props, self.param_name)
        if item is None:
            self.report({'WARNING'}, f"Param '{self.param_name}' is not in the loaded snapshot.")
            return {'CANCELLED'}
        if item.is_linked:
            self.report({'INFO'}, f"'{self.param_name}' is already linked.")
            return {'FINISHED'}
        if not item.can_create:
            self.report({'WARNING'}, item.message or f"'{self.param_name}' cannot be created.")
            return {'CANCELLED'}

        created, reused = _apply_base_read_entries(
            context,
            material,
            [_item_to_dict(item)],
            allow_export_socket=bool(self.create_export_socket),
        )
        if created == 0 and reused == 0:
            self.report({'WARNING'}, item.message or f"No change for '{self.param_name}'.")
            return {'CANCELLED'}

        if self.create_export_socket:
            message = f"Created export-only param '{self.param_name}'"
        else:
            message = f"Created helper param '{self.param_name}'"
        if reused:
            message += f" (reused {reused})"
        _refresh_base_read_snapshot(
            material,
            props.base_read_requested_path,
            count_created=created,
            status="ok",
            message=message,
        )
        self.report({'INFO'}, message)
        return {'FINISHED'}

def update_node_group_inputs(depsgraph):
    for ob in depsgraph.objects:
        mat = ob.active_material
        group_inputs = get_group_inputs(mat)
        if group_inputs:
            for input_socket in group_inputs:
                # if 'BigWaves' in input_socket.name:
                #     pass
                input_prop = next((ip for ip in mat.witcher_props.input_props if ip.name == input_socket.name), None)
                if input_prop is None:
                    input_prop = mat.witcher_props.input_props.add()
                    input_prop.name = input_socket.name
                    input_prop.type = str(input_socket.type) #set the type of the socket
                    input_prop.is_enabled_temp = input_prop.is_enabled
                if input_socket.type == 'RGBA':
                    input_prop.value = get_socket_value(input_socket)
                elif input_socket.type == 'VALUE':
                    input_prop.value = str(get_socket_value(input_socket))
                elif input_socket.type == 'VECTOR':
                    input_prop.value = str(get_socket_value(input_socket))
                else:
                    input_prop.value = str(input_socket.default_value)
                input_prop.is_linked = input_socket.is_linked
                # for pro in mat.witcher_props.input_props:
                #     pass
            # for idx, prop in enumerate(mat.witcher_props.input_props):
            #     for input in group_inputs:
            #         found = True if prop.name == input.name else False
            #     mat.witcher_props.input_props.remove(idx) if not found else None
        elif mat and mat.witcher_props and mat.witcher_props.input_props:
            pass #mat.witcher_props.input_props.clear()


class ClearInputPropsOperator(bpy.types.Operator):
    """Clear Input Props Operator"""
    bl_idname = "witcher.clear_input_props"
    bl_label = "Clear Input Props"

    def execute(self, context):
        mat = context.material
        mat.witcher_props.input_props.clear()
        depsgraph = context.evaluated_depsgraph_get()
        update_node_group_inputs(depsgraph)
        return {'FINISHED'}

class WITCH_OT_copy_texture_path(bpy.types.Operator):
    """Copy texture export path to clipboard"""
    bl_idname = "witcher.copy_texture_path"
    bl_label = "Copy Path"

    path: bpy.props.StringProperty()

    @classmethod
    def description(cls, context, properties):
        return properties.path if properties.path else "No path"

    def execute(self, context):
        context.window_manager.clipboard = self.path
        self.report({'INFO'}, f"Copied: {self.path}")
        return {'FINISHED'}

__classes = [
    ClearInputPropsOperator,
    WITCH_OT_search_base_material_path,
    WITCH_OT_use_recommended_base_material_group,
    WITCH_OT_read_base_material,
    WITCH_OT_create_missing_base_material_params,
    WITCH_OT_create_base_material_param,
    WITCH_UL_base_material_paths,
    WITCH_PT_materials,
    ReplacePrincipledBSDFOperator,
    WITCH_OT_copy_texture_path,
]

def register():
    bpy.types.Node.witcher_include = bpy.props.BoolProperty(default=False)
    bpy.types.Node.witcher_final_path = bpy.props.StringProperty(default="")
    bpy.types.Node.witcher_param_kind = bpy.props.StringProperty(default="")
    bpy.types.Node.witcher_param_name = bpy.props.StringProperty(default="")
    bpy.types.Node.witcher_vector_source = bpy.props.StringProperty(default="")
    bpy.types.Node.witcher_vector_w = bpy.props.FloatProperty(default=1.0)
    bpy.utils.register_class(NodeGroupInputProperties) #! imp to reg first
    bpy.utils.register_class(BaseMaterialPathItem)
    bpy.utils.register_class(BaseMaterialParamItem)
    bpy.utils.register_class(WitcherMaterialProperties)
    bpy.types.Material.witcher_props = bpy.props.PointerProperty(type=WitcherMaterialProperties)

    
    for __class in __classes:
        bpy.utils.register_class(__class)
    #bpy.app.handlers.depsgraph_update_post.append(update_node_group_inputs)


    #bpy.utils.register_class(MyNodeMenu)
    #bpy.types.SpaceNodeEditor.draw_handler_add(open_menu, (), 'WINDOW', 'POST_PIXEL')
    
    # bpy.utils.register_class(MoveTexturesPanel)
    # bpy.utils.register_class(MoveTexturesOperator)
    # bpy.types.Scene.path_a = bpy.props.StringProperty(name="Path A", description="Source Path")
    # bpy.types.Scene.path_b = bpy.props.StringProperty(name="Path B", description="Destination Path")

    
def unregister():
    # bpy.utils.unregister_class(MoveTexturesPanel)
    # bpy.utils.unregister_class(MoveTexturesOperator)
    # del bpy.types.Scene.path_a
    # del bpy.types.Scene.path_b
    
    for __class in __classes:
        bpy.utils.unregister_class(__class)
    bpy.utils.unregister_class(WitcherMaterialProperties)
    bpy.utils.unregister_class(BaseMaterialParamItem)
    bpy.utils.unregister_class(BaseMaterialPathItem)
    bpy.utils.unregister_class(NodeGroupInputProperties) #! imp to reg first
    # if update_node_group_inputs in bpy.app.handlers.depsgraph_update_post:
    #     bpy.app.handlers.depsgraph_update_post.remove(update_node_group_inputs)
    #for handle in bpy.app.handlers.depsgraph_update_post:
    del bpy.types.Material.witcher_props
    del bpy.types.Node.witcher_include
    del bpy.types.Node.witcher_final_path
    del bpy.types.Node.witcher_param_kind
    del bpy.types.Node.witcher_param_name
    del bpy.types.Node.witcher_vector_source
    del bpy.types.Node.witcher_vector_w
    #bpy.types.SpaceNodeEditor.draw_handler_remove(open_menu, 'WINDOW')
