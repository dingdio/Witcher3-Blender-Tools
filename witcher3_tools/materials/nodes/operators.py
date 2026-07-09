"""Blender operators for Witcher material nodes."""

import os
import subprocess
from types import SimpleNamespace

import bpy

from ...CR2W.common_blender import win_safe_path, win_unprefix_path
from ...repo_paths import normalize_source_game as _normalize_material_source_game
from ..base_path import inspect_material_base_path
from ..material import (
    ensure_node_group_for_recommendation,
    find_group_input_socket,
    get_active_witcher_group_node,
    init_material_nodes,
)
from ..reader import normalize_depot_path
from .domain import (
    _apply_base_read_entries,
    _apply_chain_frames,
    _apply_chain_item_colors_to_nodes,
    _auto_resolve_texarray_group_source_path,
    _auto_resolve_texture_node_source_path,
    _base_path_group_recommendation,
    _base_read_is_stale,
    _demote_primary_node_from_local,
    _ensure_material_chain_shader_group,
    _filtered_material_base_paths,
    _find_base_read_param_item,
    _find_linked_param_for_node,
    _is_base_read_auto_create_dict,
    _is_base_read_auto_create_entry,
    _is_user_created_linked_node,
    _item_to_dict,
    _iter_chain_source_nodes,
    _iter_local_nodes,
    _layout_chain_nodes_by_inventory,
    _layout_chain_nodes_by_source,
    _linked_primary_for_param_name,
    _linked_socket_type_validation_issue,
    _material_base_path_values,
    _material_source_game,
    _node_group_family_name,
    _nodes_upstream_of_active_group,
    _promote_primary_node_to_local,
    _refresh_base_read_snapshot,
    _remove_chain_frames,
    _remove_user_linked_param_graph,
    _resolve_source_location_path,
    _set_base_read_snapshot,
    _short_path_label,
    _source_kind_label,
    _sync_base_read_snapshot_state,
    auto_load_base_material_snapshot,
    update_node_group_inputs,
    validate_material_export_params,
)
from .properties import BaseMaterialPathItem


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



class WITCH_OT_search_base_material_path(bpy.types.Operator):
    bl_idname = "witcher.search_base_material_path"
    bl_label = "Search Base Path"
    bl_description = "Search source-specific .w2mi and .w2mg paths to populate the Base Path"
    bl_options = {'REGISTER', 'INTERNAL'}

    source_game: bpy.props.EnumProperty(
        name="Game",
        items=[
            ('w3', "Witcher 3", "Search Witcher 3 bundle material paths"),
            ('w2', "Witcher 2", "Search Witcher 2 REDkit/Uncook material paths"),
        ],
        default='w3',
    )
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

    def _rebuild_items(self, context):
        matches, _total = _filtered_material_base_paths(
            self.filter_text,
            file_type=self.file_type,
            source_game=self.source_game,
            context=context,
        )
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

        material_game = _material_source_game(material)
        self.source_game = _normalize_material_source_game(self.source_game)
        if self.source_game == "w3" and material_game == "w2":
            self.source_game = "w2"
        current = normalize_depot_path(getattr(material.witcher_props, "base_custom", ""))
        self.filter_text = ""
        self.file_type = 'ALL'

        if not _material_base_path_values(source_game=self.source_game, context=context):
            self.report({'WARNING'}, f"No {self.source_game.upper()} .w2mi or .w2mg paths were found")
            return {'CANCELLED'}

        self._rebuild_items(context)
        if current and self.base_path_items:
            for idx, item in enumerate(self.base_path_items):
                if normalize_depot_path(getattr(item, "path", "")) == current:
                    self.base_path_items_index = idx
                    break

        return context.window_manager.invoke_props_dialog(self, width=980)

    def check(self, context):
        self.source_game = _normalize_material_source_game(self.source_game)
        self._rebuild_items(context)
        return True

    def draw(self, context):
        layout = self.layout
        source_row = layout.row(align=True)
        source_row.prop(self, "source_game", expand=True)
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
        material.witcher_props.material_version = "witcher2" if self.source_game == "w2" else "witcher3"
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

        ng = ensure_node_group_for_recommendation(recommendation)
        node_ng.node_tree = ng
        if recommendation.get("shader_type"):
            node_ng.label = recommendation["shader_type"]
        material.witcher_props.node_group_name = ng.name
        self.report({'INFO'}, f"Updated active group to {ng.name}")
        return {'FINISHED'}


class WITCH_OT_load_base_material_snapshot(bpy.types.Operator):
    """One-click Base Path snapshot load (same pass imports used to run automatically)."""
    bl_idname = "witcher.load_base_material_snapshot"
    bl_label = "Load Material Chain Snapshot"
    bl_description = "Read the Base Path chain and load the Material Chain snapshot for this material"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        material = context.material
        if material is None or getattr(material, "witcher_props", None) is None:
            self.report({'ERROR'}, "No material selected")
            return {'CANCELLED'}
        inspection = auto_load_base_material_snapshot(context, material, create_missing=True)
        if inspection.get("errors"):
            self.report({'WARNING'}, str(inspection["errors"][0]))
            return {'CANCELLED'}
        return {'FINISHED'}


class WITCH_OT_read_base_material(bpy.types.Operator):
    bl_idname = "witcher.read_base_material"
    bl_label = "Load"
    bl_description = "Read the Base Path chain, create preview params, and create export-only sockets for explicit .w2mi overrides"
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

        layout.label(text="Read the Base Path and create missing supported params.", icon='INFO')
        layout.label(text="Explicit .w2mi overrides get export-only sockets if needed.", icon='LINKED')
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
        counts_box.label(text=f"Missing Preview Params: {counts.get('available', 0)}")
        counts_box.label(text=f"Already Linked: {counts.get('present', 0)}")
        counts_box.label(text=f"Export-Only: {counts.get('unsupported', 0)}")

        note_box = layout.box()
        note_box.label(text="Any existing nodes are preserved.", icon='CHECKMARK')
        if not inspection.get("has_active_witcher_group"):
            note_box.label(text="Load will create/connect the recommended Witcher shader group.", icon='NODETREE')
            note_box.label(text="The current Material Output surface link will be disconnected.", icon='LINKED')

    def execute(self, context):
        material = context.material
        if material is None:
            self.report({'ERROR'}, "No material selected")
            return {'CANCELLED'}

        _group, _created_group, group_message = _ensure_material_chain_shader_group(material)
        inspection = inspect_material_base_path(material)
        self._cached_inspection = inspection
        if inspection.get("errors"):
            message = str(inspection["errors"][0])
            _set_base_read_snapshot(material, inspection, status="error", message=message, count_created=0)
            self.report({'ERROR'}, message)
            return {'CANCELLED'}

        entries = [
            entry for entry in inspection.get("inventory", []) or []
            if _is_base_read_auto_create_dict(entry)
        ]
        created, reused = _apply_base_read_entries(context, material, entries, allow_export_socket=True)

        message = f"Loaded Base Path snapshot. Created {created} helper node(s)"
        if reused:
            message += f", reused {reused}"
        if group_message:
            message += f". {group_message}"
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
    bl_label = "Create Missing Base Material Params"
    bl_description = "Create missing preview params plus export-only sockets for explicit .w2mi overrides"
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
            if _is_base_read_auto_create_entry(item)
        ]
        created, reused = _apply_base_read_entries(context, material, entries, allow_export_socket=True)
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

    @classmethod
    def description(cls, context, properties):
        if getattr(properties, "create_export_socket", False):
            return (
                "Create a helper node plus a local export socket. "
                "Use this when the current shader group has no preview input for the param, "
                "but the param should still be available as a material instance override."
            )
        return "Create a helper node and connect it to the matching shader group input."

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


class WITCH_OT_layout_base_material_chain_nodes(bpy.types.Operator):
    bl_idname = "witcher.layout_base_material_chain_nodes"
    bl_label = "Sort Nodes by Value"
    bl_description = "Lay out material-chain nodes as one row per effective value"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        material = context.material or getattr(getattr(context, "object", None), "active_material", None)
        if material is None or getattr(material, "node_tree", None) is None:
            self.report({'ERROR'}, "No material selected")
            return {'CANCELLED'}
        props = getattr(material, "witcher_props", None)
        if props is None or not getattr(props, "base_read_params", None):
            self.report({'WARNING'}, "Load the Base Path before sorting chain nodes")
            return {'CANCELLED'}

        inspection = {"inventory": [_item_to_dict(item) for item in props.base_read_params]}
        _layout_chain_nodes_by_inventory(material, inspection)
        _apply_chain_item_colors_to_nodes(material)
        if bool(getattr(props, "base_read_chain_frames_enabled", True)):
            _apply_chain_frames(material, create_missing=True)
        self.report({'INFO'}, "Sorted material nodes by effective value")
        return {'FINISHED'}


class WITCH_OT_sort_base_material_chain_nodes(bpy.types.Operator):
    bl_idname = "witcher.sort_base_material_chain_nodes"
    bl_label = "Sort Nodes by W2MI"
    bl_description = "Group linked nodes by .w2mi/.w2mg chain source from top to bottom"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        material = context.material or getattr(getattr(context, "object", None), "active_material", None)
        if material is None or getattr(material, "node_tree", None) is None:
            self.report({'ERROR'}, "No material selected")
            return {'CANCELLED'}
        props = getattr(material, "witcher_props", None)
        if props is None or not getattr(props, "base_read_chain", None):
            self.report({'WARNING'}, "Load the Base Path before sorting chain nodes")
            return {'CANCELLED'}

        moved_count = _layout_chain_nodes_by_source(material)
        _apply_chain_item_colors_to_nodes(material)
        if bool(getattr(props, "base_read_chain_frames_enabled", True)):
            _apply_chain_frames(material, create_missing=True)
        if moved_count == 0:
            self.report({'INFO'}, "No linked material-chain nodes found to sort")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Sorted {moved_count} node(s) by material chain")
        return {'FINISHED'}


class WITCH_OT_frame_base_material_chain_nodes(bpy.types.Operator):
    bl_idname = "witcher.frame_base_material_chain_nodes"
    bl_label = "Frame Chain Nodes"
    bl_description = "Toggle frames around nodes from each .w2mi/.w2mg chain source"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        material = context.material or getattr(getattr(context, "object", None), "active_material", None)
        if material is None or getattr(material, "node_tree", None) is None:
            self.report({'ERROR'}, "No material selected")
            return {'CANCELLED'}
        props = getattr(material, "witcher_props", None)
        if props is None or not getattr(props, "base_read_chain", None):
            self.report({'WARNING'}, "Load the Base Path before framing chain nodes")
            return {'CANCELLED'}

        if bool(getattr(props, "base_read_chain_frames_enabled", True)):
            props.base_read_chain_frames_enabled = False
            removed_count = _remove_chain_frames(material)
            self.report({'INFO'}, f"Removed {removed_count} material-chain frame(s)")
            return {'FINISHED'}

        props.base_read_chain_frames_enabled = True
        _layout_chain_nodes_by_source(material)
        _apply_chain_item_colors_to_nodes(material)
        framed_count = _apply_chain_frames(material, create_missing=True)
        if framed_count == 0:
            self.report({'INFO'}, "No linked material-chain nodes found to frame")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Framed {framed_count} node(s) by material chain")
        return {'FINISHED'}


class WITCH_OT_select_base_material_local_nodes(bpy.types.Operator):
    bl_idname = "witcher.select_base_material_local_nodes"
    bl_label = "Select Local Nodes"
    bl_description = "Select nodes promoted to local material overrides"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        material = context.material or getattr(getattr(context, "object", None), "active_material", None)
        if material is None or getattr(material, "node_tree", None) is None:
            self.report({'ERROR'}, "No material selected")
            return {'CANCELLED'}

        nodes = material.node_tree.nodes
        selected_count = 0
        active_node = None
        for node in nodes:
            try:
                node.select = False
            except Exception:
                pass
        for node in _iter_local_nodes(material, linked_only=True) or []:
            try:
                node.select = True
                active_node = node
                selected_count += 1
            except Exception:
                continue
        if active_node is not None:
            try:
                nodes.active = active_node
            except Exception:
                pass
        if selected_count == 0:
            self.report({'INFO'}, "No local override nodes found")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Selected {selected_count} local node(s)")
        return {'FINISHED'}


class WITCH_OT_promote_base_material_param_to_local(bpy.types.Operator):
    bl_idname = "witcher.promote_base_material_param_to_local"
    bl_label = "Toggle Local"
    bl_description = "Toggle this linked material-chain value as a local exported override"
    bl_options = {'REGISTER', 'UNDO'}

    param_name: bpy.props.StringProperty(name="Param")

    @classmethod
    def description(cls, context, properties):
        param_name = getattr(properties, "param_name", "") or ""
        if not param_name:
            return cls.bl_description
        material = context.material or getattr(getattr(context, "object", None), "active_material", None)
        if material is not None:
            _, primary_node = _linked_primary_for_param_name(material, param_name)
            if primary_node is not None and bool(getattr(primary_node, "witcher_include", False)):
                return f"Remove {param_name} from local exported overrides"
        return f"Promote {param_name} to a local exported override"

    def invoke(self, context, event):
        material = context.material or getattr(getattr(context, "object", None), "active_material", None)
        if material is not None:
            _, primary_node = _linked_primary_for_param_name(material, self.param_name)
            if primary_node is not None and bool(getattr(primary_node, "witcher_include", False)):
                return context.window_manager.invoke_confirm(self, event)
        return self.execute(context)

    def execute(self, context):
        material = context.material or getattr(getattr(context, "object", None), "active_material", None)
        if material is None or getattr(material, "node_tree", None) is None:
            self.report({'ERROR'}, "No material selected")
            return {'CANCELLED'}

        input_socket, primary_node = _linked_primary_for_param_name(material, self.param_name)
        if input_socket is None or primary_node is None:
            self.report({'WARNING'}, f"No linked node found for '{self.param_name}'")
            return {'CANCELLED'}

        if bool(getattr(primary_node, "witcher_include", False)):
            demoted_count = _demote_primary_node_from_local(material, primary_node)
            if demoted_count == 0:
                self.report({'WARNING'}, f"Could not remove '{self.param_name}' from local")
                return {'CANCELLED'}
            self.report({'INFO'}, f"Removed '{self.param_name}' from local")
            return {'FINISHED'}

        type_issue = _linked_socket_type_validation_issue(material, input_socket, primary_node)
        if type_issue:
            self.report({'WARNING'}, f"Cannot promote: {type_issue}")
            return {'CANCELLED'}

        tagged_count = _promote_primary_node_to_local(material, input_socket, primary_node)
        if tagged_count == 0:
            self.report({'WARNING'}, f"Could not promote '{self.param_name}'")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Promoted '{self.param_name}' to local")
        return {'FINISHED'}


class WITCH_OT_promote_selected_material_node_to_local(bpy.types.Operator):
    bl_idname = "witcher.promote_selected_material_node_to_local"
    bl_label = "Toggle Selected Local"
    bl_description = "Toggle the selected linked material node as a local exported override"
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        material = context.material or getattr(getattr(context, "object", None), "active_material", None)
        nodes = getattr(getattr(material, "node_tree", None), "nodes", None)
        if material is not None and nodes is not None:
            selected_nodes = [node for node in nodes if bool(getattr(node, "select", False))]
            for node in selected_nodes:
                _, primary_node = _find_linked_param_for_node(material, node)
                if primary_node is not None and bool(getattr(primary_node, "witcher_include", False)):
                    return context.window_manager.invoke_confirm(self, event)
        return self.execute(context)

    def execute(self, context):
        material = context.material or getattr(getattr(context, "object", None), "active_material", None)
        if material is None or getattr(material, "node_tree", None) is None:
            self.report({'ERROR'}, "No material selected")
            return {'CANCELLED'}

        nodes = material.node_tree.nodes
        selected_nodes = []
        active_node = getattr(nodes, "active", None)
        if active_node is not None and bool(getattr(active_node, "select", False)):
            selected_nodes.append(active_node)
        selected_nodes.extend([
            node for node in nodes
            if bool(getattr(node, "select", False)) and node is not active_node
        ])
        if not selected_nodes:
            self.report({'WARNING'}, "Select a material node to promote")
            return {'CANCELLED'}

        for node in selected_nodes:
            input_socket, primary_node = _find_linked_param_for_node(material, node)
            if input_socket is None or primary_node is None:
                continue
            if bool(getattr(primary_node, "witcher_include", False)):
                demoted_count = _demote_primary_node_from_local(material, primary_node)
                if demoted_count:
                    self.report({'INFO'}, f"Removed '{input_socket.name}' from local")
                    return {'FINISHED'}
                continue
            type_issue = _linked_socket_type_validation_issue(material, input_socket, primary_node)
            if type_issue:
                self.report({'WARNING'}, f"Cannot promote: {type_issue}")
                return {'CANCELLED'}
            tagged_count = _promote_primary_node_to_local(material, input_socket, primary_node)
            if tagged_count:
                self.report({'INFO'}, f"Promoted '{input_socket.name}' to local")
                return {'FINISHED'}

        self.report({'WARNING'}, "Selected node is not linked to the active Witcher shader group")
        return {'CANCELLED'}


class WITCH_OT_replace_user_material_param_with_chain(bpy.types.Operator):
    bl_idname = "witcher.replace_user_material_param_with_chain"
    bl_label = "Replace User Node With Chain Value"
    bl_description = "Disconnect the user-created linked node and recreate this value from the Material Chain"
    bl_options = {'REGISTER', 'UNDO'}

    param_name: bpy.props.StringProperty(name="Param")

    @classmethod
    def description(cls, context, properties):
        param_name = getattr(properties, "param_name", "") or ""
        if param_name:
            return f"Replace the user-created node linked to {param_name} with the Material Chain value"
        return cls.bl_description

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        material = context.material or getattr(getattr(context, "object", None), "active_material", None)
        if material is None or getattr(material, "node_tree", None) is None or getattr(material, "witcher_props", None) is None:
            self.report({'ERROR'}, "No material selected")
            return {'CANCELLED'}

        props = material.witcher_props
        if props.base_read_status != "ok" or not props.base_read_requested_path:
            self.report({'WARNING'}, "Read the Material Chain first.")
            return {'CANCELLED'}
        if _base_read_is_stale(props):
            self.report({'WARNING'}, "The loaded Material Chain snapshot is stale. Read the current Base Path again.")
            return {'CANCELLED'}

        item = _find_base_read_param_item(props, self.param_name)
        if item is None:
            self.report({'WARNING'}, f"Param '{self.param_name}' is not in the loaded snapshot.")
            return {'CANCELLED'}
        if not bool(getattr(item, "has_value", False)) or not bool(getattr(item, "is_supported", False)):
            self.report({'WARNING'}, f"'{self.param_name}' has no supported chain value to recreate.")
            return {'CANCELLED'}

        input_socket, primary_node = _linked_primary_for_param_name(material, self.param_name)
        if input_socket is None or primary_node is None:
            self.report({'WARNING'}, f"No linked user node found for '{self.param_name}'")
            return {'CANCELLED'}
        if not _is_user_created_linked_node(primary_node):
            self.report({'WARNING'}, f"'{self.param_name}' is already using a Material Chain node.")
            return {'CANCELLED'}

        entry = _item_to_dict(item)
        entry["is_linked"] = False
        entry["can_create"] = True
        unlinked_count, removed_count = _remove_user_linked_param_graph(material, input_socket, primary_node)
        if unlinked_count == 0:
            self.report({'WARNING'}, f"Could not disconnect '{self.param_name}'")
            return {'CANCELLED'}

        created, reused = _apply_base_read_entries(
            context,
            material,
            [entry],
            allow_export_socket=not bool(entry.get("has_matching_socket", False)),
        )
        if created == 0 and reused == 0:
            self.report({'WARNING'}, f"Disconnected user node, but could not recreate '{self.param_name}' from the chain")
            return {'CANCELLED'}

        message = f"Replaced user node for '{self.param_name}'"
        if removed_count:
            message += f" and removed {removed_count} node(s)"
        _refresh_base_read_snapshot(
            material,
            props.base_read_requested_path,
            count_created=created,
            status="ok",
            message=message,
        )
        self.report({'INFO'}, message)
        return {'FINISHED'}


class WITCH_OT_select_base_material_param_node(bpy.types.Operator):
    bl_idname = "witcher.select_base_material_param_node"
    bl_label = "Select Linked Param Node"
    bl_description = "Select the node linked to this material parameter"
    bl_options = {'REGISTER', 'UNDO'}

    param_name: bpy.props.StringProperty(name="Param")

    @classmethod
    def description(cls, context, properties):
        param_name = getattr(properties, "param_name", "") or ""
        return f"Select the node linked to {param_name}" if param_name else cls.bl_description

    def execute(self, context):
        material = context.material or getattr(getattr(context, "object", None), "active_material", None)
        if material is None or getattr(material, "node_tree", None) is None:
            self.report({'ERROR'}, "No material selected")
            return {'CANCELLED'}

        node_ng = get_active_witcher_group_node(material)
        if node_ng is None:
            self.report({'WARNING'}, "No active Witcher shader group is connected")
            return {'CANCELLED'}

        input_pin = find_group_input_socket(node_ng, self.param_name)
        if input_pin is None or not getattr(input_pin, "is_linked", False) or not input_pin.links:
            self.report({'INFO'}, f"No linked node found for '{self.param_name}'")
            return {'CANCELLED'}

        linked_node = input_pin.links[0].from_node
        if linked_node is None:
            self.report({'INFO'}, f"No linked node found for '{self.param_name}'")
            return {'CANCELLED'}

        nodes = material.node_tree.nodes
        for node in nodes:
            try:
                node.select = False
            except Exception:
                pass
        try:
            linked_node.select = True
            nodes.active = linked_node
        except Exception:
            self.report({'WARNING'}, f"Could not select node for '{self.param_name}'")
            return {'CANCELLED'}

        self.report({'INFO'}, f"Selected node for '{self.param_name}'")
        return {'FINISHED'}


class WITCH_OT_select_base_material_chain_nodes(bpy.types.Operator):
    bl_idname = "witcher.select_base_material_chain_nodes"
    bl_label = "Select Chain Nodes"
    bl_description = "Select only nodes created from this material-chain entry"
    bl_options = {'REGISTER', 'UNDO'}

    source_path: bpy.props.StringProperty(name="Source Path")
    source_index: bpy.props.IntProperty(name="Source Index", default=-1)

    def execute(self, context):
        material = context.material or getattr(getattr(context, "object", None), "active_material", None)
        if material is None or getattr(material, "node_tree", None) is None:
            self.report({'ERROR'}, "No material selected")
            return {'CANCELLED'}

        target_path = normalize_depot_path(self.source_path)
        target_index = int(self.source_index)
        upstream = _nodes_upstream_of_active_group(material)
        if upstream is None:
            self.report({'WARNING'}, "No active Witcher shader group is connected")
            return {'CANCELLED'}
        nodes = material.node_tree.nodes
        selected_count = 0
        active_node = None

        for node in nodes:
            try:
                node.select = False
            except Exception:
                pass

        for node in _iter_chain_source_nodes(material, target_path, target_index, linked_only=True) or []:
            try:
                node.select = True
                active_node = node
                selected_count += 1
            except Exception:
                continue

        if active_node is not None:
            try:
                nodes.active = active_node
            except Exception:
                pass

        if selected_count == 0:
            label = self.source_path or f"source index {target_index}"
            self.report({'INFO'}, f"No linked nodes found for {label}")
            return {'CANCELLED'}

        self.report({'INFO'}, f"Selected {selected_count} node(s)")
        return {'FINISHED'}


class WITCH_OT_open_base_material_chain_location(bpy.types.Operator):
    bl_idname = "witcher.open_base_material_chain_location"
    bl_label = "Open Material Location"
    bl_description = "Open the folder containing this material or texture file"
    bl_options = {'REGISTER', 'INTERNAL'}

    source_path: bpy.props.StringProperty(name="Source Path")

    @classmethod
    def description(cls, context, properties):
        path = getattr(properties, "source_path", "") or ""
        return f"Show {path} on disk" if path else cls.bl_description

    def _open_path(self, disk_path: str) -> bool:
        disk_path = win_unprefix_path(os.path.normpath(disk_path))
        safe_path = win_safe_path(disk_path)
        if os.path.isfile(safe_path) and os.name == 'nt':
            explorer_path = disk_path.replace('"', '\\"')
            subprocess.Popen(f'explorer.exe /select,"{explorer_path}"')
            return True

        folder = disk_path if os.path.isdir(safe_path) else os.path.dirname(disk_path)
        bpy.ops.wm.path_open(filepath=folder)
        return False

    def execute(self, context):
        source_path = str(self.source_path or "")
        if not source_path:
            self.report({'WARNING'}, "No path to open")
            return {'CANCELLED'}

        material = context.material or getattr(getattr(context, "object", None), "active_material", None)
        props = getattr(material, "witcher_props", None) if material else None
        repo_version = 115 if str(getattr(props, "material_version", "") or "").lower() == "witcher2" else 999

        disk_path = _resolve_source_location_path(context, source_path, repo_version)
        if not disk_path:
            self.report({'WARNING'}, f"File not found: {source_path}")
            return {'CANCELLED'}

        disk_path = win_unprefix_path(disk_path)
        safe_path = win_safe_path(disk_path)
        folder = disk_path if os.path.isdir(safe_path) else os.path.dirname(disk_path)
        if not folder:
            self.report({'WARNING'}, f"Could not find folder for: {source_path}")
            return {'CANCELLED'}

        try:
            selected_file = self._open_path(disk_path)
        except Exception as exc:
            self.report({'ERROR'}, f"Failed to open material location: {exc}")
            return {'CANCELLED'}

        self.report({'INFO'}, f"Selected: {disk_path}" if selected_file else f"Opened: {folder}")
        return {'FINISHED'}



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


class WITCH_OT_material_chain_help(bpy.types.Operator):
    bl_idname = "witcher.material_chain_help"
    bl_label = "Material Chain Help"
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=360)

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        col.label(text="Swatch: source or local node color")
        col.label(text="Plus: create or promote to Local")
        col.label(text="Check: linked or already Local")
        col.label(text="User icon: manually linked node")
        col.label(text="Refresh: replace user node with chain value")
        col.label(text="Export Params shows Local and user-linked params")
        col.label(text="Expanded rows include full paths and copy/open actions")

    def execute(self, context):
        return {'FINISHED'}


class WITCH_OT_validate_material_export_params(bpy.types.Operator):
    bl_idname = "witcher.validate_material_export_params"
    bl_label = "Validate Export Params"
    bl_description = "Validate Local export params before writing the mesh"
    bl_options = {'INTERNAL'}

    issues_text: bpy.props.StringProperty(default="")

    def invoke(self, context, event):
        material = context.material or getattr(getattr(context, "object", None), "active_material", None)
        issues = validate_material_export_params(material)
        self.issues_text = "\n".join(issues) if issues else "No export param type issues found."
        return context.window_manager.invoke_props_dialog(self, width=440)

    def draw(self, context):
        col = self.layout.column(align=True)
        for line in str(self.issues_text or "").splitlines():
            col.label(text=_short_path_label(line, 120), icon='ERROR' if ": expected " in line or ": missing " in line else 'CHECKMARK')

    def execute(self, context):
        return {'FINISHED'}


class WITCH_OT_autoresolve_texture_repo_path(bpy.types.Operator):
    bl_idname = "witcher.autoresolve_texture_repo_path"
    bl_label = "Auto Resolve Texture Repo Path"
    bl_description = "Resolve the connected texture file to a game repo path and fill the texture path"
    bl_options = {'REGISTER', 'UNDO'}

    param_name: bpy.props.StringProperty(name="Param")

    @classmethod
    def description(cls, context, properties):
        param_name = getattr(properties, "param_name", "") or ""
        return f"Auto resolve the repo path for {param_name}" if param_name else cls.bl_description

    def execute(self, context):
        material = context.material or getattr(getattr(context, "object", None), "active_material", None)
        if material is None:
            self.report({'ERROR'}, "No material selected")
            return {'CANCELLED'}
        input_socket, primary_node = _linked_primary_for_param_name(material, self.param_name)
        if input_socket is None or primary_node is None:
            self.report({'WARNING'}, f"No linked texture found for '{self.param_name}'")
            return {'CANCELLED'}

        node_type = str(getattr(primary_node, "type", "") or "")
        if node_type == 'GROUP':
            repo_path = _auto_resolve_texarray_group_source_path(primary_node, force=True)
        elif node_type in {'TEX_IMAGE', 'TEX_ENVIRONMENT'}:
            repo_path = _auto_resolve_texture_node_source_path(primary_node, force=True)
        else:
            self.report({'WARNING'}, f"'{self.param_name}' is not linked to a texture node")
            return {'CANCELLED'}

        if not repo_path:
            self.report({'WARNING'}, f"Could not resolve a repo path for '{self.param_name}'")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Resolved '{self.param_name}' to {repo_path}")
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


OPERATOR_CLASSES = (
    ClearInputPropsOperator,
    WITCH_OT_material_chain_help,
    WITCH_OT_validate_material_export_params,
    WITCH_OT_autoresolve_texture_repo_path,
    WITCH_OT_search_base_material_path,
    WITCH_OT_use_recommended_base_material_group,
    WITCH_OT_load_base_material_snapshot,
    WITCH_OT_read_base_material,
    WITCH_OT_create_missing_base_material_params,
    WITCH_OT_create_base_material_param,
    WITCH_OT_layout_base_material_chain_nodes,
    WITCH_OT_sort_base_material_chain_nodes,
    WITCH_OT_frame_base_material_chain_nodes,
    WITCH_OT_select_base_material_local_nodes,
    WITCH_OT_promote_base_material_param_to_local,
    WITCH_OT_promote_selected_material_node_to_local,
    WITCH_OT_replace_user_material_param_with_chain,
    WITCH_OT_select_base_material_param_node,
    WITCH_OT_select_base_material_chain_nodes,
    WITCH_OT_open_base_material_chain_location,
    ReplacePrincipledBSDFOperator,
    WITCH_OT_copy_texture_path,
)


def register():
    for cls in OPERATOR_CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(OPERATOR_CLASSES):
        bpy.utils.unregister_class(cls)
