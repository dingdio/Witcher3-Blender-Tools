import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from bpy.types import Material, Node

from .w3_material import (
    build_param_element,
    create_node_for_param,
    find_group_input_socket,
    get_active_witcher_group_node,
    node_tree_inputs_new,
)
from .w3_material_constants import IGNORED_PARAMS, PARAM_ORDER
from .w3_material_reader import (
    collect_material_chain,
    read_declared_graph_params,
    read_local_material_params_from_bin,
)

log = logging.getLogger(__name__)

SUPPORTED_BASE_MATERIAL_PARAM_TYPES: Set[str] = {
    'Color',
    'Float',
    'Vector',
    'handle:ITexture',
    'handle:CTextureArray',
    'handle:CCubeTexture',
}

_PARAM_SOCKET_TYPES: Dict[str, str] = {
    'Color': 'NodeSocketColor',
    'Float': 'NodeSocketFloat',
    'Vector': 'NodeSocketVector',
    'handle:ITexture': 'NodeSocketColor',
    'handle:CTextureArray': 'NodeSocketColor',
    'handle:CCubeTexture': 'NodeSocketColor',
}


def _ordered_param_names(names: Set[str]) -> List[str]:
    return sorted(
        names,
        key=lambda name: (
            PARAM_ORDER.index(name) if name in PARAM_ORDER else len(PARAM_ORDER),
            str(name).lower(),
        ),
    )


def _build_inventory_entry(
        *,
        name: str,
        param_type: str,
        value: str,
        source_kind: str,
        source_path: str,
        node_ng: Optional[Node],
        material_ready: bool,
        is_declared_only: bool = False,
        has_value: bool = True,
        message: str = "",
    ) -> Dict[str, Any]:
    is_supported = bool(param_type in SUPPORTED_BASE_MATERIAL_PARAM_TYPES)
    is_ignored = bool(name in IGNORED_PARAMS)
    input_pin = find_group_input_socket(node_ng, name) if node_ng else None
    has_matching_socket = bool(input_pin)
    is_linked = bool(input_pin and len(input_pin.links) != 0)

    if is_declared_only or not has_value:
        status = "declared_only_info"
        can_create = False
        if not message:
            message = "Declared in graph metadata only."
    elif is_linked:
        status = "present_linked"
        can_create = False
        if not message:
            message = "Already linked on the active shader group."
    elif is_ignored:
        status = "ignored_info"
        can_create = False
        if not message:
            message = "Ignored by the current material node builder."
    elif not material_ready:
        status = "available_to_create" if is_supported else "declared_only_info"
        can_create = False
        if not message:
            message = "No active Witcher shader group is connected to Material Output."
    elif has_matching_socket:
        status = "available_to_create"
        can_create = is_supported
        if not message:
            message = "Create a helper node and connect it to the empty socket."
    elif is_supported:
        status = "unsupported_export_only"
        can_create = True
        if not message:
            message = "Create as an export-only socket on a material-local group copy."
    else:
        status = "declared_only_info"
        can_create = False
        if not message:
            message = f"Unsupported parameter type '{param_type}'."

    return {
        "name": name,
        "param_type": param_type or "",
        "value": value if value is not None else "",
        "source_kind": source_kind or "",
        "source_path": source_path or "",
        "has_value": bool(has_value),
        "has_matching_socket": has_matching_socket,
        "is_linked": is_linked,
        "is_supported": is_supported,
        "is_declared_only": bool(is_declared_only),
        "can_create": bool(can_create),
        "status": status,
        "message": message,
    }


def build_material_inventory(material_path: str, material: Optional[Material] = None) -> Dict[str, Any]:
    chain_info = collect_material_chain(material_path)
    node_ng = get_active_witcher_group_node(material) if material else None
    material_ready = bool(node_ng)

    effective_params: Dict[str, Dict[str, str]] = {}
    resolved_graph = str(chain_info.get("resolved_graph") or "")
    chain = chain_info.get("chain", [])

    graph_entry = next((entry for entry in chain if entry.get("chunk_type") == "CMaterialGraph"), None)
    if graph_entry is not None:
        graph_params = read_local_material_params_from_bin(graph_entry.get("_material_bin"))
        for par_name, attrs in graph_params.items():
            effective_params[par_name] = {
                "param_type": attrs[0],
                "value": attrs[1],
                "source_kind": "graph_default",
                "source_path": graph_entry.get("path", ""),
            }

    instance_entries = [
        entry for entry in reversed(chain)
        if entry.get("chunk_type") == "CMaterialInstance"
    ]
    for entry in instance_entries:
        local_params = read_local_material_params_from_bin(entry.get("_material_bin"))
        for par_name, attrs in local_params.items():
            effective_params[par_name] = {
                "param_type": attrs[0],
                "value": attrs[1],
                "source_kind": "instance",
                "source_path": entry.get("path", ""),
            }

    declared_graph_params = read_declared_graph_params(resolved_graph or material_path) or set()
    inventory: List[Dict[str, Any]] = []
    for par_name in _ordered_param_names(set(effective_params.keys())):
        entry = effective_params[par_name]
        inventory.append(
            _build_inventory_entry(
                name=par_name,
                param_type=entry.get("param_type", ""),
                value=entry.get("value", ""),
                source_kind=entry.get("source_kind", ""),
                source_path=entry.get("source_path", ""),
                node_ng=node_ng,
                material_ready=material_ready,
            )
        )

    declared_only_names = _ordered_param_names(set(declared_graph_params) - set(effective_params.keys()))
    for par_name in declared_only_names:
        inventory.append(
            _build_inventory_entry(
                name=par_name,
                param_type="",
                value="",
                source_kind="declared_only",
                source_path=resolved_graph or material_path,
                node_ng=node_ng,
                material_ready=material_ready,
                is_declared_only=True,
                has_value=False,
            )
        )

    counts = {
        "concrete": sum(1 for item in inventory if not item["is_declared_only"]),
        "declared_only": sum(1 for item in inventory if item["is_declared_only"]),
        "present": sum(1 for item in inventory if item["status"] == "present_linked"),
        "available": sum(
            1 for item in inventory
            if item["status"] == "available_to_create" and item["can_create"]
        ),
        "unsupported": sum(1 for item in inventory if item["status"] == "unsupported_export_only"),
    }

    warnings = list(chain_info.get("warnings", []))
    if material and not material_ready:
        warnings.append("No active Witcher shader group connected to Material Output; creation actions are disabled.")

    return {
        "requested_path": chain_info.get("requested_path", ""),
        "normalized_path": chain_info.get("normalized_path", ""),
        "resolved_graph": resolved_graph,
        "chain": [
            {
                "path": entry.get("path", ""),
                "chunk_type": entry.get("chunk_type", ""),
                "source_kind": entry.get("source_kind", ""),
            }
            for entry in chain
        ],
        "effective_params": effective_params,
        "declared_graph_params": sorted(declared_graph_params),
        "inventory": inventory,
        "counts": counts,
        "warnings": warnings,
        "errors": list(chain_info.get("errors", [])),
        "has_active_witcher_group": material_ready,
        "active_group_node_name": getattr(node_ng, "name", "") if node_ng else "",
    }


def inspect_material_base_path(material: Material, material_path: Optional[str] = None) -> Dict[str, Any]:
    requested_path = material_path
    if requested_path is None and material is not None:
        requested_path = getattr(getattr(material, "witcher_props", None), "base_custom", "")
    return build_material_inventory(requested_path or "", material=material)


def refresh_base_material_entry_state(material: Optional[Material], entry: Dict[str, Any]) -> Dict[str, Any]:
    node_ng = get_active_witcher_group_node(material) if material else None
    return _build_inventory_entry(
        name=str(entry.get("name", "") or ""),
        param_type=str(entry.get("param_type", "") or ""),
        value=str(entry.get("value", "") or ""),
        source_kind=str(entry.get("source_kind", "") or ""),
        source_path=str(entry.get("source_path", "") or ""),
        node_ng=node_ng,
        material_ready=bool(node_ng),
        is_declared_only=bool(entry.get("is_declared_only", False)),
        has_value=bool(entry.get("has_value", False)),
    )


def tag_base_material_helper_node(node, param_name: str, source_path: str = "", source_kind: str = ""):
    if not node:
        return
    try:
        node["witcher_base_material_helper"] = True
        node["witcher_base_material_param"] = param_name
        node["witcher_base_material_source"] = source_path or ""
        node["witcher_base_material_source_kind"] = source_kind or ""
    except Exception:
        pass


def _base_material_helper_nodes(material: Optional[Material], param_name: str = "") -> List[Node]:
    if material is None or getattr(material, "node_tree", None) is None:
        return []
    matches: List[Node] = []
    for node in material.node_tree.nodes:
        try:
            if not bool(node.get("witcher_base_material_helper")):
                continue
            if param_name and str(node.get("witcher_base_material_param", "") or "") != param_name:
                continue
            matches.append(node)
        except Exception:
            continue
    return matches


def _socket_type_for_param(param_type: str) -> str:
    return _PARAM_SOCKET_TYPES.get(param_type, "")


def ensure_local_material_group_node(material: Material, node_ng: Optional[Node]) -> Optional[Node]:
    if material is None or node_ng is None or node_ng.type != 'GROUP' or getattr(node_ng, "node_tree", None) is None:
        return node_ng
    node_tree = node_ng.node_tree
    try:
        if bool(node_tree.get("witcher_material_local_copy")):
            return node_ng
    except Exception:
        pass

    node_tree_copy = node_tree.copy()
    node_tree_copy.use_fake_user = False
    try:
        node_tree_copy["witcher_material_local_copy"] = True
        node_tree_copy["witcher_material_owner"] = material.name
    except Exception:
        pass
    node_ng.node_tree = node_tree_copy
    return node_ng


def ensure_group_input_socket(node_ng: Optional[Node], param_name: str, param_type: str):
    if node_ng is None:
        return None
    input_pin = find_group_input_socket(node_ng, param_name)
    if input_pin is not None:
        return input_pin
    socket_type = _socket_type_for_param(param_type)
    if not socket_type:
        return None
    node_tree_inputs_new(node_ng, socket_type, param_name)
    return find_group_input_socket(node_ng, param_name)


def _relink_base_material_helper_node(
        material: Material,
        node_ng: Node,
        helper_node: Node,
        input_pin,
        param_type: str,
        ) -> bool:
    if material is None or node_ng is None or helper_node is None or input_pin is None:
        return False
    if len(input_pin.links) != 0 or not getattr(helper_node, "outputs", None):
        return False
    primary_output = helper_node.outputs[0]
    if len(primary_output.links) != 0:
        return False

    try:
        material.node_tree.links.new(primary_output, input_pin)
        if param_type == 'handle:ITexture' and helper_node.type == 'TEX_IMAGE':
            alpha_pin = node_ng.inputs.get(f"{input_pin.name}_alpha")
            if alpha_pin and len(alpha_pin.links) == 0 and len(helper_node.outputs) > 1 and len(helper_node.outputs[1].links) == 0:
                material.node_tree.links.new(helper_node.outputs[1], alpha_pin)
        helper_node.witcher_include = False
        return True
    except Exception as exc:
        log.warning("Failed to relink base material helper '%s': %s", getattr(helper_node, "name", "<unnamed>"), exc)
        return False


def _next_helper_y(mat: Material) -> int:
    nodes = mat.node_tree.nodes
    if not nodes:
        return 1000
    min_y = min(int(getattr(node.location, "y", node.location[1])) for node in nodes)
    return min_y - 170


def _ordered_group_inputs(node_ng: Optional[Node]) -> List:
    if node_ng is None:
        return []
    input_names = {
        str(getattr(input_socket, "name", "") or "")
        for input_socket in getattr(node_ng, "inputs", []) or []
    }
    return [
        input_socket for input_socket in getattr(node_ng, "inputs", []) or []
        if not (
            str(getattr(input_socket, "name", "") or "").endswith("_W")
            and str(getattr(input_socket, "name", "") or "")[:-2] in input_names
        )
    ]


def _layout_group_inputs(node_ng: Optional[Node], target_param_name: str = "") -> List:
    ordered_inputs = _ordered_group_inputs(node_ng)
    if not ordered_inputs:
        return []
    return [
        input_socket for input_socket in ordered_inputs
        if str(getattr(input_socket, "name", "") or "") == target_param_name
        or (
            getattr(input_socket, "is_linked", False)
            and len(getattr(input_socket, "links", [])) != 0
        )
    ]


def _param_step_for_type(param_type: str) -> int:
    if param_type == 'handle:ITexture':
        return 320
    if param_type == 'Color':
        return 220
    return 170


def _param_step_for_node(node: Optional[Node], param_type: str = "") -> int:
    if node is not None:
        if node.type == 'TEX_IMAGE':
            return 320
        if node.type == 'RGB':
            return 220
    return _param_step_for_type(param_type)


def _base_material_param_type_map(material: Optional[Material]) -> Dict[str, str]:
    param_types: Dict[str, str] = {}
    if material is None:
        return param_types
    props = getattr(material, "witcher_props", None)
    if props is None:
        return param_types
    for item in getattr(props, "base_read_params", []) or []:
        name = str(getattr(item, "name", "") or "")
        if not name:
            continue
        param_types[name] = str(getattr(item, "param_type", "") or "")
    return param_types


def _desired_helper_y(
        material: Material,
        node_ng: Optional[Node],
        param_name: str,
        param_type: str,
        ) -> int:
    layout_inputs = _layout_group_inputs(node_ng, param_name)
    if not layout_inputs:
        return _next_helper_y(material)

    target_index = next((
        idx for idx, input_socket in enumerate(layout_inputs)
        if str(getattr(input_socket, "name", "") or "") == param_name
    ), None)
    if target_index is None:
        return _next_helper_y(material)

    param_types = _base_material_param_type_map(material)
    if param_name and param_name not in param_types:
        param_types[param_name] = param_type

    def step_for_index(index: int) -> int:
        input_socket = layout_inputs[index]
        input_name = str(getattr(input_socket, "name", "") or "")
        if getattr(input_socket, "is_linked", False) and len(getattr(input_socket, "links", [])) != 0:
            linked_node = input_socket.links[0].from_socket.node
            return _param_step_for_node(linked_node, param_types.get(input_name, ""))
        return _param_step_for_type(param_types.get(input_name, ""))

    prev_index = next((
        idx for idx in range(target_index - 1, -1, -1)
        if getattr(layout_inputs[idx], "is_linked", False) and len(getattr(layout_inputs[idx], "links", [])) != 0
    ), None)
    next_index = next((
        idx for idx in range(target_index + 1, len(layout_inputs))
        if getattr(layout_inputs[idx], "is_linked", False) and len(getattr(layout_inputs[idx], "links", [])) != 0
    ), None)

    y_from_prev = None
    if prev_index is not None:
        prev_node = layout_inputs[prev_index].links[0].from_socket.node
        y_from_prev = int(getattr(prev_node.location, "y", prev_node.location[1]))
        for idx in range(prev_index, target_index):
            y_from_prev -= step_for_index(idx)

    y_from_next = None
    if next_index is not None:
        next_node = layout_inputs[next_index].links[0].from_socket.node
        y_from_next = int(getattr(next_node.location, "y", next_node.location[1]))
        for idx in range(target_index, next_index):
            y_from_next += step_for_index(idx)

    if y_from_prev is not None:
        return y_from_prev
    if y_from_next is not None:
        return y_from_next

    y_loc = 1000
    for idx in range(target_index):
        y_loc -= step_for_index(idx)
    return y_loc


def _desired_helper_x(node_ng: Optional[Node], param_name: str) -> int:
    layout_inputs = _layout_group_inputs(node_ng, param_name)
    if layout_inputs:
        target_index = next((
            idx for idx, input_socket in enumerate(layout_inputs)
            if str(getattr(input_socket, "name", "") or "") == param_name
        ), None)
        if target_index is not None:
            prev_index = next((
                idx for idx in range(target_index - 1, -1, -1)
                if getattr(layout_inputs[idx], "is_linked", False) and len(getattr(layout_inputs[idx], "links", [])) != 0
            ), None)
            next_index = next((
                idx for idx in range(target_index + 1, len(layout_inputs))
                if getattr(layout_inputs[idx], "is_linked", False) and len(getattr(layout_inputs[idx], "links", [])) != 0
            ), None)
            if prev_index is not None:
                prev_node = layout_inputs[prev_index].links[0].from_socket.node
                return int(getattr(prev_node.location, "x", prev_node.location[0]))
            if next_index is not None:
                next_node = layout_inputs[next_index].links[0].from_socket.node
                return int(getattr(next_node.location, "x", next_node.location[0]))

    if node_ng is None:
        return -450
    return int(getattr(node_ng.location, "x", node_ng.location[0])) - 950


def _shift_new_param_nodes_x(material: Material, created_node_ptrs: Set[int], target_x: int, primary_node: Optional[Node]) -> None:
    if material is None or primary_node is None or not created_node_ptrs:
        return
    current_x = int(getattr(primary_node.location, "x", primary_node.location[0]))
    delta_x = target_x - current_x
    if delta_x == 0:
        return

    for node in material.node_tree.nodes:
        try:
            if node.as_pointer() not in created_node_ptrs:
                continue
            node.location.x = int(getattr(node.location, "x", node.location[0])) + delta_x
        except Exception:
            continue


def create_base_material_helper(
        material: Material,
        entry: Dict[str, Any],
        uncook_path: str,
        *,
        node_ng: Optional[Node] = None,
        allow_export_socket: bool = False,
        y_loc: Optional[int] = None,
        ) -> Tuple[Optional[Node], Optional[Node], str]:
    if material is None:
        return node_ng, None, "missing_material"
    if node_ng is None:
        node_ng = get_active_witcher_group_node(material)
    if node_ng is None:
        return None, None, "missing_group"

    par_name = str(entry.get("name", "") or "")
    par_type = str(entry.get("param_type", "") or "")
    par_value = str(entry.get("value", "") or "")
    if not par_name or not par_value or par_value == "NULL":
        return node_ng, None, "invalid_param"

    input_pin = find_group_input_socket(node_ng, par_name)
    if input_pin is None and allow_export_socket:
        node_ng = ensure_local_material_group_node(material, node_ng)
        input_pin = ensure_group_input_socket(node_ng, par_name, par_type)

    if input_pin is not None and len(input_pin.links) != 0:
        return node_ng, input_pin.links[0].from_socket.node, "already_linked"

    if input_pin is not None:
        for helper_node in _base_material_helper_nodes(material, param_name=par_name):
            if _relink_base_material_helper_node(material, node_ng, helper_node, input_pin, par_type):
                tag_base_material_helper_node(
                    helper_node,
                    par_name,
                    source_path=str(entry.get("source_path", "") or ""),
                    source_kind=str(entry.get("source_kind", "") or ""),
                )
                return node_ng, helper_node, "reused"

    if y_loc is None:
        y_loc = _desired_helper_y(material, node_ng, par_name, par_type)
    desired_x = _desired_helper_x(node_ng, par_name)
    existing_node_ptrs = {node.as_pointer() for node in material.node_tree.nodes}

    param = build_param_element(
        par_name,
        par_type,
        par_value,
        witcher_require_socket="true",
    )
    node = create_node_for_param(material, param, node_ng, uncook_path, y_loc)
    if node is None:
        return node_ng, None, "skipped"
    created_node_ptrs = {
        created_node.as_pointer()
        for created_node in material.node_tree.nodes
        if created_node.as_pointer() not in existing_node_ptrs
    }
    _shift_new_param_nodes_x(material, created_node_ptrs, desired_x, node)

    node.witcher_include = False
    tag_base_material_helper_node(
        node,
        par_name,
        source_path=str(entry.get("source_path", "") or ""),
        source_kind=str(entry.get("source_kind", "") or ""),
    )
    return node_ng, node, "created"
