import json
import logging
import re

import bpy
from .CR2W.witcher_cache.Bundles import LoadBundleManager
from .w3_material import (
    ensure_node_group,
    find_group_input_socket,
    get_active_witcher_group_node,
    get_recommended_node_group_for_base_path,
    init_material_nodes,
)
from .w3_material_base_path import (
    create_base_material_helper,
    inspect_material_base_path,
    refresh_base_material_entry_state,
)
from .w3_material_chain import (
    CHAIN_NODE_ROW_Y,
    LOCAL_NODE_COLOR,
    chain_color_for_index,
    chain_node_x,
    chain_node_y,
    chain_row_step_for_type,
    coerce_source_index,
    local_node_x,
)
from .w3_material_reader import normalize_depot_path
from .w3_material_constants import (
    DEFAULT_W2_MATERIAL_BASE,
    DEFAULT_W3_MATERIAL_BASE,
    WITCHER2_MATERIALS,
)
from .w3_vector_param import (
    get_legacy_w_value,
    get_mapping_vector_input,
    get_vector_node_values,
    is_vector_param_node,
    mark_vector_param_node,
)
from . import get_all_addon_prefs, get_texture_path, get_uncook_path
from .extension_paths import get_cache_root
from .repo_paths import (
    normalize_source_game as _normalize_material_source_game,
    w2_source_repo_root_if_configured,
    w2_source_roots,
)
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

log = logging.getLogger(__name__)

_BASE_PATH_CACHE_FILE = Path(get_cache_root(create=True)) / "material_base_paths.json"
_BASE_PATH_ENUM_CACHE = {}
_NUMERIC_SUFFIX_RE = re.compile(r"\.\d{3}$")
_TEXARRAY_SLICE_RE = re.compile(r"(?i)^(.+?\.texarray)\.texture_\d+\.[^\\/]+$")
_TEXTURE_LOCATION_EXTS = (".xbm", ".dds", ".tga", ".png")


def _material_source_game(material) -> str:
    props = getattr(material, "witcher_props", None)
    version = str(getattr(props, "material_version", "") or "").strip().lower()
    return "w2" if version == "witcher2" else "w3"


def _base_path_cache_file(source_game="w3") -> Path:
    source_game = _normalize_material_source_game(source_game)
    if source_game == "w3":
        return _BASE_PATH_CACHE_FILE
    return Path(get_cache_root(create=True)) / f"material_base_paths_{source_game}.json"


def _cache_base_path_enum_items(cache_key, items):
    stable_items = []
    for item in items or [("", "No base materials found", "")]:
        identifier = str(item[0] or "")
        label = str(item[1] or identifier or "No base materials found")
        description = str(item[2] or "")
        stable_items.append((identifier, label, description))
    _BASE_PATH_ENUM_CACHE[cache_key] = stable_items
    return stable_items


def _load_material_base_path_cache(source_game="w3"):
    cache_file = _base_path_cache_file(source_game)
    try:
        if not cache_file.exists():
            return []
        with open(cache_file, 'r', encoding='utf-8') as handle:
            payload = json.load(handle)
        paths = payload.get("paths", [])
        if not isinstance(paths, list):
            return []
        return [str(path) for path in paths if str(path or "").strip()]
    except Exception:
        log.warning("Failed to load material base path cache from %s", cache_file, exc_info=True)
        return []


def _save_material_base_path_cache(paths, source_game="w3"):
    cache_file = _base_path_cache_file(source_game)
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_file, 'w', encoding='utf-8') as handle:
            json.dump({"paths": list(paths)}, handle, indent=2)
    except Exception:
        log.warning("Failed to save material base path cache to %s", cache_file, exc_info=True)


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


def _gather_w2_material_base_paths_from_roots(context):
    paths = {
        normalize_depot_path(identifier)
        for identifier, _label, _description in WITCHER2_MATERIALS
        if identifier and identifier != "custom"
    }
    for root in w2_source_roots(context, existing_only=True):
        try:
            for dirpath, _dirnames, filenames in os.walk(root):
                for filename in filenames:
                    if not filename.lower().endswith((".w2mi", ".w2mg")):
                        continue
                    full_path = os.path.join(dirpath, filename)
                    rel_path = os.path.relpath(full_path, root)
                    paths.add(normalize_depot_path(rel_path))
        except Exception:
            log.debug("Failed to scan Witcher 2 material base paths under %s", root, exc_info=True)
    return sorted(paths, key=str.lower)


def _material_base_path_enum_items(force_refresh: bool = False, *, source_game="w3", context=None):
    source_game = _normalize_material_source_game(source_game)
    cache_key = f"{source_game}_material_base_paths"
    if not force_refresh and cache_key in _BASE_PATH_ENUM_CACHE:
        return _BASE_PATH_ENUM_CACHE[cache_key]

    paths = [] if force_refresh else _load_material_base_path_cache(source_game)
    if not paths:
        try:
            if source_game == "w2":
                paths = _gather_w2_material_base_paths_from_roots(context)
            else:
                paths = _gather_material_base_paths_from_bundles()
        except Exception:
            log.warning("Failed to gather %s base material paths", source_game.upper(), exc_info=True)
            paths = []
        if paths:
            _save_material_base_path_cache(paths, source_game)

    items = [
        (
            path,
            path,
            f"Bundle material path ({Path(path).name})",
        )
        for path in paths
    ]
    return _cache_base_path_enum_items(cache_key, items)


def _material_base_path_values(force_refresh: bool = False, *, source_game="w3", context=None):
    return [
        identifier
        for identifier, _label, _description in _material_base_path_enum_items(
            force_refresh,
            source_game=source_game,
            context=context,
        )
        if identifier
    ]


def _filtered_material_base_paths(query: str = "", *, file_type: str = "ALL", limit: int = 0, source_game="w3", context=None):
    paths = _material_base_path_values(source_game=source_game, context=context)
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


def _active_material_output_node(material):
    if material is None or getattr(material, "node_tree", None) is None:
        return None
    nodes = material.node_tree.nodes
    output_node = next(
        (node for node in nodes if node.type == 'OUTPUT_MATERIAL' and bool(getattr(node, "is_active_output", True))),
        None,
    )
    if output_node is None:
        output_node = next((node for node in nodes if node.type == 'OUTPUT_MATERIAL'), None)
    return output_node


def _find_material_group_node(material, node_group_name: str):
    if material is None or getattr(material, "node_tree", None) is None:
        return None
    target_family = _node_group_family_name(SimpleNamespace(name=node_group_name))
    for node in material.node_tree.nodes:
        if node.type != 'GROUP' or getattr(node, "node_tree", None) is None:
            continue
        if _node_group_family_name(node.node_tree) == target_family:
            return node
    return None


def _ensure_material_chain_shader_group(material) -> tuple[object, bool, str]:
    """Create/connect the recommended Witcher shader group without clearing user nodes."""
    if material is None:
        return None, False, "No material selected"
    material.use_nodes = True
    if getattr(material, "node_tree", None) is None:
        return None, False, "Material has no node tree"

    active_group = get_active_witcher_group_node(material)
    if active_group is not None:
        return active_group, False, ""

    recommendation = _base_path_group_recommendation(material)
    recommended_name = str((recommendation or {}).get("node_group_name", "") or "")
    if not recommended_name:
        return None, False, "Base Path does not resolve to a recommended node group"

    nodes = material.node_tree.nodes
    links = material.node_tree.links
    output_node = _active_material_output_node(material)
    if output_node is None:
        output_node = nodes.new(type='ShaderNodeOutputMaterial')
        output_node.location = (900, 200)
    surface_input = output_node.inputs.get("Surface")
    if surface_input is None:
        return None, False, "Material Output has no Surface input"

    node_group = _find_material_group_node(material, recommended_name)
    created = False
    if node_group is None:
        try:
            ng = ensure_node_group(recommended_name, resource_path=recommendation.get("resource_path"))
        except Exception as exc:
            return None, False, f"Could not load node group {recommended_name}: {exc}"
        node_group = nodes.new(type='ShaderNodeGroup')
        node_group.node_tree = ng
        node_group.label = str(recommendation.get("shader_type", "") or recommended_name)
        node_group.width = 350
        created = True

    if node_group.outputs:
        for link in list(getattr(surface_input, "links", []) or []):
            links.remove(link)
        links.new(node_group.outputs[0], surface_input)
    else:
        return None, created, f"Node group {recommended_name} has no outputs"

    try:
        input_x = min((float(getattr(node.location, "x", 0.0)) for node in nodes if node is not node_group), default=500.0)
        output_x = float(getattr(output_node.location, "x", 900.0))
        node_group.location.x = min(output_x - 420.0, input_x + 420.0)
        node_group.location.y = float(getattr(output_node.location, "y", 200.0))
    except Exception:
        node_group.location = (500, 200)

    material.witcher_props.node_group_name = getattr(node_group.node_tree, "name", recommended_name)
    action = "Created" if created else "Connected"
    return node_group, created, f"{action} {node_group.node_tree.name}"


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
        "graph": "Graph",
        "graph_default": "Graph Default",
        "declared_only": "Declared Only",
    }
    return labels.get(str(source_kind or ""), str(source_kind or "") or "Unknown")


def _compact_param_type_label(param_type: str) -> str:
    text = str(param_type or "")
    if text.startswith("handle:"):
        return text.split(":", 1)[1]
    return text


BASE_READ_VALUE_TYPE_FILTER_ITEMS = [
    ('ALL', "All", "Show all material-chain values"),
    ('FLOAT', "Float", "Show Float params"),
    ('COLOR', "Color", "Show Color params"),
    ('VECTOR', "Vector", "Show Vector params"),
    ('ITEXTURE', "ITexture", "Show texture params"),
    ('TEXARRAY', "CTextureArray", "Show texture-array params"),
    ('CUBE', "CCubeTexture", "Show cubemap params"),
    ('OTHER', "Other", "Show other params"),
]


EXPORT_PARAMS_SORT_MODE_ITEMS = [
    ('TYPE', "Type", "Group Export Params by value type"),
    ('SOCKET', "Socket", "Show Export Params in shader socket order"),
]


def _param_type_filter_key(param_type: str) -> str:
    label = _compact_param_type_label(param_type)
    if label == "Float":
        return 'FLOAT'
    if label == "Color":
        return 'COLOR'
    if label == "Vector":
        return 'VECTOR'
    if label == "ITexture":
        return 'ITEXTURE'
    if label == "CTextureArray":
        return 'TEXARRAY'
    if label == "CCubeTexture":
        return 'CUBE'
    return 'OTHER'


def _base_read_item_matches_value_filters(item, search_text: str, type_filter: str) -> bool:
    if type_filter and type_filter != 'ALL':
        if _param_type_filter_key(getattr(item, "param_type", "")) != type_filter:
            return False

    query = normalize_depot_path(search_text).strip()
    if not query:
        return True

    param_type = getattr(item, "param_type", "")
    source_path = getattr(item, "source_path", "")
    haystack_parts = [
        getattr(item, "name", ""),
        param_type,
        _compact_param_type_label(param_type),
        source_path,
        _source_file_label(source_path),
        getattr(item, "source_kind", ""),
        getattr(item, "value", ""),
        getattr(item, "status", ""),
        getattr(item, "message", ""),
    ]
    haystack = normalize_depot_path(" ".join(str(part or "") for part in haystack_parts))
    return query in haystack


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
        "source_index": coerce_source_index(getattr(item, "source_index", -1)),
        "row_index": coerce_source_index(getattr(item, "row_index", -1)),
        "row_y": coerce_source_index(getattr(item, "row_y", -1)),
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


_BASE_CHAIN_COLOR_UPDATE_SUSPENDED = False


def _node_material_source_path(node) -> str:
    try:
        return str(
            node.get("witcher_material_source_path")
            or node.get("witcher_base_material_source")
            or ""
        )
    except Exception:
        return ""


def _node_material_source_index(node) -> int:
    try:
        return coerce_source_index(node.get("witcher_material_source_index", -1))
    except Exception:
        return -1


def _node_material_source_param(node) -> str:
    try:
        return str(
            node.get("witcher_material_source_param")
            or node.get("witcher_base_material_param")
            or ""
        )
    except Exception:
        return ""


def _node_has_material_chain_source(node) -> bool:
    if node is None:
        return False
    try:
        if bool(node.get("witcher_base_material_helper")):
            return True
        if _node_material_source_param(node):
            return True
        if _node_material_source_path(node):
            return True
        if _node_material_source_index(node) >= 0:
            return True
    except Exception:
        return False
    return False


def _is_user_created_linked_node(node) -> bool:
    if node is None:
        return False
    try:
        if bool(node.get("witcher_material_user_local")):
            return True
    except Exception:
        pass
    return not _node_has_material_chain_source(node)


def _is_local_override_node(node) -> bool:
    try:
        return bool(getattr(node, "witcher_include", False)) or bool(node.get("witcher_material_local_override"))
    except Exception:
        return False


def _nodes_upstream_of_active_group(material):
    node_ng = get_active_witcher_group_node(material)
    if node_ng is None:
        return None

    upstream = set()
    stack = []
    for input_socket in getattr(node_ng, "inputs", []) or []:
        if not getattr(input_socket, "is_linked", False):
            continue
        for link in getattr(input_socket, "links", []) or []:
            from_node = getattr(link, "from_node", None)
            if from_node is not None:
                stack.append(from_node)

    while stack:
        node = stack.pop()
        try:
            node_id = node.as_pointer()
        except Exception:
            node_id = id(node)
        if node_id in upstream:
            continue
        upstream.add(node_id)
        for input_socket in getattr(node, "inputs", []) or []:
            if not getattr(input_socket, "is_linked", False):
                continue
            for link in getattr(input_socket, "links", []) or []:
                from_node = getattr(link, "from_node", None)
                if from_node is not None:
                    stack.append(from_node)
    return upstream


def _chain_source_matches_node(node, source_path: str, source_index: int) -> bool:
    if _is_local_override_node(node):
        return False
    target_path = normalize_depot_path(source_path)
    node_path = normalize_depot_path(_node_material_source_path(node))
    if target_path and node_path == target_path:
        return True
    target_index = coerce_source_index(source_index)
    return bool(target_index >= 0 and _node_material_source_index(node) == target_index)


def _iter_chain_source_nodes(material, source_path: str, source_index: int, *, linked_only: bool = True):
    if material is None or getattr(material, "node_tree", None) is None:
        return
    upstream = _nodes_upstream_of_active_group(material) if linked_only else None
    if linked_only and upstream is None:
        return

    for node in material.node_tree.nodes:
        if linked_only:
            try:
                if node.as_pointer() not in upstream:
                    continue
            except Exception:
                continue
        if _chain_source_matches_node(node, source_path, source_index):
            yield node


def _chain_color_from_nodes(material, source_path: str, source_index: int, fallback):
    for node in _iter_chain_source_nodes(material, source_path, source_index, linked_only=True) or []:
        try:
            if getattr(node, "use_custom_color", False):
                return tuple(float(value) for value in node.color[:3])
        except Exception:
            continue
    return fallback


def _set_chain_item_node_color(item, color) -> None:
    global _BASE_CHAIN_COLOR_UPDATE_SUSPENDED
    _BASE_CHAIN_COLOR_UPDATE_SUSPENDED = True
    try:
        item.node_color = tuple(float(value) for value in color[:3])
    finally:
        _BASE_CHAIN_COLOR_UPDATE_SUSPENDED = False


def _chain_item_color_key(path: str, source_index: int):
    return (normalize_depot_path(path), coerce_source_index(source_index))


def _capture_chain_item_colors(props) -> dict:
    colors = {}
    for item in getattr(props, "base_read_chain", []) or []:
        key = _chain_item_color_key(getattr(item, "path", ""), getattr(item, "source_index", -1))
        try:
            colors[key] = tuple(float(value) for value in getattr(item, "node_color", (0.5, 0.5, 0.5))[:3])
        except Exception:
            continue
    return colors


def _material_has_chain_item(material, chain_item) -> bool:
    props = getattr(material, "witcher_props", None)
    if props is None:
        return False
    try:
        target_ptr = chain_item.as_pointer()
    except Exception:
        target_ptr = None
    for item in getattr(props, "base_read_chain", []) or []:
        if target_ptr is not None:
            try:
                if item.as_pointer() == target_ptr:
                    return True
            except Exception:
                pass
        if (
            normalize_depot_path(getattr(item, "path", "")) == normalize_depot_path(getattr(chain_item, "path", ""))
            and coerce_source_index(getattr(item, "source_index", -1)) == coerce_source_index(getattr(chain_item, "source_index", -1))
        ):
            return True
    return False


def _material_for_chain_item(context, chain_item):
    candidates = [
        getattr(context, "material", None),
        getattr(getattr(context, "object", None), "active_material", None),
    ]
    for material in candidates:
        if material is not None and _material_has_chain_item(material, chain_item):
            return material
    for material in bpy.data.materials:
        if _material_has_chain_item(material, chain_item):
            return material
    return None


def _material_for_witcher_props(context, props):
    owner = getattr(props, "id_data", None)
    if isinstance(owner, bpy.types.Material):
        return owner
    try:
        props_ptr = props.as_pointer()
    except Exception:
        props_ptr = None

    candidates = [
        getattr(context, "material", None),
        getattr(getattr(context, "object", None), "active_material", None),
    ]
    for material in candidates:
        mat_props = getattr(material, "witcher_props", None)
        if mat_props is props:
            return material
        if props_ptr is not None:
            try:
                if mat_props is not None and mat_props.as_pointer() == props_ptr:
                    return material
            except Exception:
                pass
    for material in bpy.data.materials:
        mat_props = getattr(material, "witcher_props", None)
        if mat_props is props:
            return material
        if props_ptr is not None:
            try:
                if mat_props is not None and mat_props.as_pointer() == props_ptr:
                    return material
            except Exception:
                pass
    return None


def _material_for_node(context, node):
    try:
        target_id = node.as_pointer()
    except Exception:
        target_id = None

    def material_has_node(material) -> bool:
        if material is None or getattr(material, "node_tree", None) is None:
            return False
        for candidate in material.node_tree.nodes:
            if candidate is node:
                return True
            if target_id is not None:
                try:
                    if candidate.as_pointer() == target_id:
                        return True
                except Exception:
                    pass
        return False

    candidates = [
        getattr(context, "material", None),
        getattr(getattr(context, "object", None), "active_material", None),
    ]
    for material in candidates:
        if material_has_node(material):
            return material
    for material in bpy.data.materials:
        if material_has_node(material):
            return material
    return None


def _update_node_witcher_include(self, context):
    material = _material_for_node(context, self)
    if material is None:
        return
    _sync_local_override_nodes(material)
    _apply_chain_item_colors_to_nodes(material)
    if bool(getattr(getattr(material, "witcher_props", None), "base_read_chain_frames_enabled", True)):
        _layout_chain_nodes_by_source(material)
        _apply_chain_frames(material, create_missing=True)
    else:
        _remove_chain_frames(material)


def _apply_chain_color_to_nodes(material, source_path: str, source_index: int, color) -> int:
    count = 0
    node_color = tuple(float(value) for value in color[:3])
    source_index = coerce_source_index(source_index)
    _sync_local_override_nodes(material)
    for node in _iter_chain_source_nodes(material, source_path, source_index, linked_only=True) or []:
        try:
            node.use_custom_color = True
            node.color = node_color
            node["witcher_material_chain_source_index"] = int(source_index)
            count += 1
        except Exception:
            continue
    return count


def _apply_chain_item_colors_to_nodes(material) -> None:
    props = getattr(material, "witcher_props", None)
    if props is None:
        return
    for item in getattr(props, "base_read_chain", []) or []:
        _apply_chain_color_to_nodes(
            material,
            str(getattr(item, "path", "") or ""),
            coerce_source_index(getattr(item, "source_index", -1)),
            getattr(item, "node_color", (0.5, 0.5, 0.5)),
        )


def _sync_chain_item_color_from_nodes(material, item) -> None:
    source_index = coerce_source_index(getattr(item, "source_index", -1))
    fallback = chain_color_for_index(source_index) or (0.5, 0.5, 0.5)
    color = _chain_color_from_nodes(
        material,
        str(getattr(item, "path", "") or ""),
        source_index,
        fallback,
    )
    _set_chain_item_node_color(item, color)


def _sync_chain_colors_from_nodes(material) -> None:
    props = getattr(material, "witcher_props", None)
    if props is None:
        return
    for item in getattr(props, "base_read_chain", []) or []:
        _sync_chain_item_color_from_nodes(material, item)


def _update_base_material_chain_color(self, context):
    if _BASE_CHAIN_COLOR_UPDATE_SUSPENDED:
        return
    material = _material_for_chain_item(context, self)
    if material is None:
        return
    _apply_chain_color_to_nodes(
        material,
        str(getattr(self, "path", "") or ""),
        coerce_source_index(getattr(self, "source_index", -1)),
        getattr(self, "node_color", (0.5, 0.5, 0.5)),
    )
    if bool(getattr(getattr(material, "witcher_props", None), "base_read_chain_frames_enabled", True)):
        _apply_chain_frames(material, create_missing=True)


def _update_base_material_local_color(self, context):
    material = _material_for_witcher_props(context, self)
    if material is None:
        return
    _sync_local_override_nodes(material)
    local_color = _local_node_color(material)
    for node in _iter_local_nodes(material, linked_only=True) or []:
        try:
            node.use_custom_color = True
            node.color = local_color
        except Exception:
            continue
    if bool(getattr(self, "base_read_chain_frames_enabled", True)):
        _apply_chain_frames(material, create_missing=True)


def _tag_node_with_chain_source(node, source_info: dict) -> None:
    if node is None or not source_info:
        return
    source_path = str(source_info.get("source_path", "") or "")
    source_kind = str(source_info.get("source_kind", "") or "")
    param_name = str(source_info.get("name", "") or "")
    param_type = str(source_info.get("param_type", "") or "")
    source_index = coerce_source_index(source_info.get("source_index"))
    row_index = coerce_source_index(source_info.get("row_index"))
    row_y = coerce_source_index(source_info.get("row_y"))

    try:
        existing_param = str(node.get("witcher_material_source_param", "") or "")
        existing_row = coerce_source_index(node.get("witcher_material_source_row_index"))
        if existing_param and existing_row >= 0 and existing_param != param_name:
            return

        node["witcher_material_source_path"] = source_path
        node["witcher_material_source_kind"] = source_kind
        node["witcher_material_source_param"] = param_name
        node["witcher_material_source_type"] = param_type
        node["witcher_material_source_index"] = source_index
        node["witcher_material_source_row_index"] = row_index
        node["witcher_material_source_row_y"] = row_y
        if source_path:
            node["witcher_base_material_source"] = source_path
        if source_kind:
            node["witcher_base_material_source_kind"] = source_kind

        source_color = chain_color_for_index(source_index)
        if source_color is not None and (
            not getattr(node, "use_custom_color", False)
            or node.get("witcher_material_chain_source_index") is None
        ):
            node.use_custom_color = True
            node.color = source_color
            node["witcher_material_chain_source_index"] = source_index
    except Exception:
        pass


def _source_value_floats(source_info: dict):
    try:
        return [float(part.strip()) for part in str(source_info.get("value", "") or "").split(";")]
    except Exception:
        return []


def _close_enough(a, b, tolerance=0.0001) -> bool:
    try:
        return abs(float(a) - float(b)) <= tolerance
    except Exception:
        return False


def _linked_node_matches_chain_value(link, source_info: dict) -> bool:
    node = getattr(link, "from_node", None)
    if node is None:
        return False
    param_type = str(source_info.get("param_type", "") or "")
    expected_value = str(source_info.get("value", "") or "")

    if param_type == "Float":
        try:
            actual = getattr(link.from_socket, "default_value", node.outputs[0].default_value)
            return _close_enough(actual, float(expected_value))
        except Exception:
            return True

    if param_type == "Color":
        expected = _source_value_floats(source_info)
        if len(expected) < 4:
            return True
        try:
            actual = list(node.outputs[0].default_value)
            expected = [value / 255.0 for value in expected[:4]]
            return all(_close_enough(actual[idx], expected[idx]) for idx in range(4))
        except Exception:
            return True

    if param_type == "Vector":
        expected = _source_value_floats(source_info)
        if len(expected) < 3:
            return True
        try:
            actual = get_vector_node_values(node, str(source_info.get("name", "") or ""), 1.0)
            return all(_close_enough(actual[idx], expected[idx]) for idx in range(3))
        except Exception:
            return True

    if param_type == "handle:ITexture" and getattr(node, "type", "") == 'TEX_IMAGE':
        if expected_value.lower().endswith(".texarray"):
            return True
        image = getattr(node, "image", None)
        if image is None:
            return True
        try:
            rel_path = win_unprefix_path(getattr(image, "filepath", "") or "")
            abs_path = win_unprefix_path(bpy.path.abspath(rel_path))
            actual_repo_path = normalize_depot_path(get_repo_from_abs_path(os.path.normpath(abs_path)))
            expected_repo_path = normalize_depot_path(expected_value)
            return actual_repo_path == expected_repo_path
        except Exception:
            return True

    return True


def _tag_existing_linked_chain_nodes(material, inspection: dict) -> None:
    node_ng = get_active_witcher_group_node(material)
    if node_ng is None:
        return

    source_by_param = {
        str(entry.get("name", "") or ""): entry
        for entry in inspection.get("inventory", []) or []
        if entry.get("source_path") and not entry.get("is_declared_only")
    }
    if not source_by_param:
        return

    for param_name, source_info in source_by_param.items():
        input_pin = find_group_input_socket(node_ng, param_name)
        if input_pin is None or not getattr(input_pin, "is_linked", False):
            continue

        primary_link = input_pin.links[0] if input_pin.links else None
        primary_node = getattr(primary_link, "from_node", None) if primary_link is not None else None
        if primary_node is None or not _node_has_material_chain_source(primary_node):
            continue
        if primary_link is not None and not _linked_node_matches_chain_value(primary_link, source_info):
            continue

        seen = set()
        stack = [primary_node]
        while stack:
            node = stack.pop()
            try:
                node_id = node.as_pointer()
            except Exception:
                node_id = id(node)
            if node_id in seen:
                continue
            seen.add(node_id)
            _tag_node_with_chain_source(node, source_info)

            for upstream_input in getattr(node, "inputs", []) or []:
                if not getattr(upstream_input, "is_linked", False):
                    continue
                for link in getattr(upstream_input, "links", []) or []:
                    from_node = getattr(link, "from_node", None)
                    if from_node is not None:
                        stack.append(from_node)


def _node_identity(node):
    try:
        return node.as_pointer()
    except Exception:
        return id(node)


def _node_abs_location(node):
    x = float(getattr(getattr(node, "location", None), "x", 0.0))
    y = float(getattr(getattr(node, "location", None), "y", 0.0))
    parent = getattr(node, "parent", None)
    while parent is not None:
        x += float(getattr(getattr(parent, "location", None), "x", 0.0))
        y += float(getattr(getattr(parent, "location", None), "y", 0.0))
        parent = getattr(parent, "parent", None)
    return x, y


def _set_node_abs_location(node, x: float, y: float) -> None:
    parent = getattr(node, "parent", None)
    if parent is not None:
        parent_x, parent_y = _node_abs_location(parent)
        x -= parent_x
        y -= parent_y
    node.location.x = x
    node.location.y = y


def _set_node_parent_keep_location(node, parent) -> None:
    x, y = _node_abs_location(node)
    node.parent = parent
    _set_node_abs_location(node, x, y)


def _collect_upstream_nodes(start_node):
    if start_node is None:
        return []
    ordered = []
    seen = set()
    stack = [start_node]
    while stack:
        node = stack.pop()
        node_id = _node_identity(node)
        if node_id in seen:
            continue
        seen.add(node_id)
        ordered.append(node)
        for input_socket in getattr(node, "inputs", []) or []:
            if not getattr(input_socket, "is_linked", False):
                continue
            for link in getattr(input_socket, "links", []) or []:
                from_node = getattr(link, "from_node", None)
                if from_node is not None:
                    stack.append(from_node)
    return ordered


def _linked_param_groups(material):
    node_ng = get_active_witcher_group_node(material)
    if node_ng is None:
        return []
    groups = []
    for input_socket in getattr(node_ng, "inputs", []) or []:
        if not getattr(input_socket, "is_linked", False) or not getattr(input_socket, "links", None):
            continue
        primary_node = getattr(input_socket.links[0], "from_node", None)
        if primary_node is not None:
            groups.append((input_socket, primary_node))
    return groups


def _local_node_color(material):
    props = getattr(material, "witcher_props", None)
    color = getattr(props, "base_read_local_color", LOCAL_NODE_COLOR) if props is not None else LOCAL_NODE_COLOR
    try:
        return tuple(float(value) for value in color[:3])
    except Exception:
        return LOCAL_NODE_COLOR


def _tag_local_override_group(material, input_socket, primary_node) -> int:
    if primary_node is None:
        return 0
    was_local = bool(getattr(primary_node, "witcher_include", False))
    user_created = _is_user_created_linked_node(primary_node)
    try:
        if not was_local:
            primary_node.witcher_include = True
            try:
                primary_node.witcher_export = True
            except Exception:
                pass
        if user_created:
            primary_node["witcher_material_user_local"] = True
        elif primary_node.get("witcher_material_user_local") is not None:
            del primary_node["witcher_material_user_local"]
    except Exception:
        pass
    local_color = _local_node_color(material)
    tagged_count = 0
    for node in _collect_upstream_nodes(primary_node):
        try:
            node["witcher_material_local_override"] = True
            node["witcher_material_local_param"] = str(getattr(input_socket, "name", "") or "")
            if user_created:
                node["witcher_material_local_kind"] = "user"
            elif node.get("witcher_material_local_kind") is not None:
                del node["witcher_material_local_kind"]
            node.use_custom_color = True
            node.color = local_color
            tagged_count += 1
        except Exception:
            continue
    return tagged_count


def _sync_local_override_nodes(material):
    if material is None or getattr(material, "node_tree", None) is None:
        return []
    for node in material.node_tree.nodes:
        try:
            if node.get("witcher_material_local_override") is not None:
                del node["witcher_material_local_override"]
            if node.get("witcher_material_local_param") is not None:
                del node["witcher_material_local_param"]
            if node.get("witcher_material_local_kind") is not None:
                del node["witcher_material_local_kind"]
        except Exception:
            pass

    local_groups = []
    for input_socket, primary_node in _linked_param_groups(material):
        if not bool(getattr(primary_node, "witcher_include", False)):
            continue
        _tag_local_override_group(material, input_socket, primary_node)
        local_groups.append((input_socket, primary_node))
    return local_groups


def _iter_local_nodes(material, *, linked_only: bool = True):
    if material is None or getattr(material, "node_tree", None) is None:
        return
    groups = _sync_local_override_nodes(material) if linked_only else []
    if linked_only:
        seen = set()
        for _, primary_node in groups:
            for node in _collect_upstream_nodes(primary_node):
                node_id = _node_identity(node)
                if node_id in seen:
                    continue
                seen.add(node_id)
                yield node
        return

    for node in material.node_tree.nodes:
        if _is_local_override_node(node):
            yield node


def _promote_primary_node_to_local(material, input_socket, primary_node) -> int:
    tagged_count = _tag_local_override_group(material, input_socket, primary_node)
    _layout_chain_nodes_by_source(material)
    if bool(getattr(getattr(material, "witcher_props", None), "base_read_chain_frames_enabled", True)):
        _apply_chain_frames(material, create_missing=True)
    return tagged_count


def _demote_primary_node_from_local(material, primary_node) -> int:
    if primary_node is None:
        return 0
    affected_nodes = list(_collect_upstream_nodes(primary_node))
    try:
        primary_node.witcher_include = False
        if primary_node.get("witcher_material_user_local") is not None:
            del primary_node["witcher_material_user_local"]
    except Exception:
        return 0
    _sync_local_override_nodes(material)
    for node in affected_nodes:
        try:
            node.use_custom_color = False
        except Exception:
            pass
    _apply_chain_item_colors_to_nodes(material)
    _layout_chain_nodes_by_source(material)
    if bool(getattr(getattr(material, "witcher_props", None), "base_read_chain_frames_enabled", True)):
        _apply_chain_frames(material, create_missing=True)
    else:
        _remove_chain_frames(material)
    return len(affected_nodes)


def _find_linked_param_for_node(material, target_node):
    if target_node is None:
        return None, None
    try:
        target_id = target_node.as_pointer()
    except Exception:
        target_id = id(target_node)
    for input_socket, primary_node in _linked_param_groups(material):
        for node in _collect_upstream_nodes(primary_node):
            try:
                node_id = node.as_pointer()
            except Exception:
                node_id = id(node)
            if node_id == target_id:
                return input_socket, primary_node
    return None, None


def _move_upstream_group(primary_node, target_x: float, target_y: float, moved: set) -> int:
    try:
        current_x, current_y = _node_abs_location(primary_node)
    except Exception:
        return 0
    delta_x = target_x - current_x
    delta_y = target_y - current_y
    moved_count = 0
    for node in _collect_upstream_nodes(primary_node):
        if getattr(node, "type", "") == 'FRAME':
            continue
        node_id = _node_identity(node)
        if node_id in moved:
            continue
        try:
            current_node_x, current_node_y = _node_abs_location(node)
            _set_node_abs_location(node, current_node_x + delta_x, current_node_y + delta_y)
            moved.add(node_id)
            moved_count += 1
        except Exception:
            continue
    return moved_count


def _upstream_group_bounds(primary_node):
    nodes = [
        node for node in _collect_upstream_nodes(primary_node)
        if getattr(node, "type", "") != 'FRAME'
    ]
    return _node_bounds(nodes)


def _upstream_group_x_offsets(primary_node):
    bounds = _upstream_group_bounds(primary_node)
    try:
        primary_x, _ = _node_abs_location(primary_node)
    except Exception:
        return (-320.0, 220.0)
    if bounds is None:
        width = float(getattr(primary_node, "width", 200.0) or 200.0)
        return (0.0, width)
    return (bounds[0] - primary_x, bounds[2] - primary_x)


def _layout_source_metrics_for_entries(node_ng, entries):
    metrics = {}
    for entry in entries:
        source_index = coerce_source_index(entry.get("source_index"))
        if source_index < 0:
            continue
        primary_node = _linked_primary_node_for_entry(node_ng, entry)
        if primary_node is None:
            continue
        if bool(getattr(primary_node, "witcher_include", False)):
            continue
        min_dx, max_dx = _upstream_group_x_offsets(primary_node)
        if source_index in metrics:
            current_min, current_max = metrics[source_index]
            metrics[source_index] = (min(current_min, min_dx), max(current_max, max_dx))
        else:
            metrics[source_index] = (min_dx, max_dx)
    return metrics


def _local_source_metrics(local_groups):
    metrics = None
    for _, primary_node in local_groups:
        min_dx, max_dx = _upstream_group_x_offsets(primary_node)
        if metrics is None:
            metrics = (min_dx, max_dx)
        else:
            metrics = (min(metrics[0], min_dx), max(metrics[1], max_dx))
    return metrics


def _dynamic_source_x_positions(node_group_x: int, source_indices, source_metrics, *, local_metrics=None):
    positions = {}
    column_gap = 140
    cursor_min_x = None

    if local_metrics is not None:
        local_x = local_node_x(node_group_x)
        positions["local"] = local_x
        cursor_min_x = local_x + local_metrics[0]

    for source_index in sorted({idx for idx in source_indices if coerce_source_index(idx) >= 0}):
        source_index = coerce_source_index(source_index)
        min_dx, max_dx = source_metrics.get(source_index, (-320.0, 220.0))
        target_x = chain_node_x(node_group_x, source_index)
        if cursor_min_x is not None:
            target_x = min(target_x, cursor_min_x - column_gap - max_dx)
        positions[source_index] = target_x
        cursor_min_x = target_x + min_dx

    return positions


def _row_step_for_entry_group(entry: dict, primary_node) -> int:
    base_step = chain_row_step_for_type(str(entry.get("param_type", "") or ""))
    return max(70, base_step)


def _row_step_for_local_group(input_socket, primary_node) -> int:
    node_type = str(getattr(primary_node, "type", "") or "")
    if node_type == 'GROUP':
        return chain_row_step_for_type("handle:CTextureArray")
    if node_type in {'TEX_IMAGE', 'TEX_ENVIRONMENT'}:
        return chain_row_step_for_type("handle:ITexture")
    if node_type == 'RGB':
        return chain_row_step_for_type("Color")
    if node_type == 'VALUE':
        return chain_row_step_for_type("Float")
    if str(getattr(input_socket, "type", "") or "") == 'VECTOR':
        return chain_row_step_for_type("Vector")
    return chain_row_step_for_type("")


def _layout_chain_nodes_by_inventory(material, inspection: dict) -> None:
    node_ng = get_active_witcher_group_node(material)
    if node_ng is None:
        return
    try:
        node_group_x = int(getattr(node_ng.location, "x", node_ng.location[0]))
    except Exception:
        node_group_x = 500

    entries = [
        entry for entry in inspection.get("inventory", []) or []
        if not bool(entry.get("is_declared_only", False))
    ]
    local_groups = _sync_local_override_nodes(material)
    source_metrics = _layout_source_metrics_for_entries(node_ng, entries)
    source_positions = _dynamic_source_x_positions(
        node_group_x,
        source_metrics.keys(),
        source_metrics,
        local_metrics=_local_source_metrics(local_groups) if local_groups else None,
    )

    moved = set()
    for entry in inspection.get("inventory", []) or []:
        if bool(entry.get("is_declared_only", False)):
            continue
        param_name = str(entry.get("name", "") or "")
        if not param_name:
            continue
        source_index = coerce_source_index(entry.get("source_index"))
        row_index = coerce_source_index(entry.get("row_index"))
        if source_index < 0 or row_index < 0:
            continue
        row_y = coerce_source_index(entry.get("row_y"))
        target_y = row_y if row_y != -1 else chain_node_y(row_index)

        input_pin = find_group_input_socket(node_ng, param_name)
        if input_pin is None or not getattr(input_pin, "is_linked", False) or not input_pin.links:
            continue

        primary_link = input_pin.links[0]
        if primary_link is not None and not _linked_node_matches_chain_value(primary_link, entry):
            continue
        primary_node = getattr(primary_link, "from_node", None)
        if primary_node is None:
            continue

        if bool(getattr(primary_node, "witcher_include", False)):
            target_x = source_positions.get("local", local_node_x(node_group_x))
        else:
            target_x = source_positions.get(source_index, chain_node_x(node_group_x, source_index))
        _move_upstream_group(primary_node, target_x, target_y, moved)


def _chain_source_key(path: str, source_index: int):
    return (normalize_depot_path(path), coerce_source_index(source_index))


def _entry_matches_chain_item(entry: dict, chain_item) -> bool:
    item_path = str(getattr(chain_item, "path", "") or "")
    item_index = coerce_source_index(getattr(chain_item, "source_index", -1))
    entry_path = str(entry.get("source_path", "") or "")
    entry_index = coerce_source_index(entry.get("source_index"))
    if normalize_depot_path(item_path) and normalize_depot_path(entry_path) == normalize_depot_path(item_path):
        return True
    return item_index >= 0 and entry_index == item_index


def _chain_item_for_entry(props, entry):
    entry_dict = _item_to_dict(entry) if not isinstance(entry, dict) else entry
    for candidate in getattr(props, "base_read_chain", []) or []:
        if _entry_matches_chain_item(entry_dict, candidate):
            return candidate
    return None


def _display_type_for_linked_socket(input_socket, linked_node, item=None) -> str:
    param_type = str(getattr(item, "param_type", "") or "") if item is not None else ""
    if param_type:
        return _compact_param_type_label(param_type)
    node_type = str(getattr(linked_node, "type", "") or "")
    if node_type == 'RGB':
        return "Color"
    if node_type == 'VALUE':
        return "Float"
    if node_type == 'TEX_IMAGE':
        return "ITexture"
    if node_type == 'TEX_ENVIRONMENT':
        return "CCubeTexture"
    if node_type == 'GROUP':
        return "CTextureArray"
    socket_type = str(getattr(input_socket, "type", "") or "")
    if socket_type:
        return socket_type.title()
    return node_type


def _export_param_type_group(input_socket, linked_node, item=None, *, store: bool = True) -> tuple[str, str, str, int]:
    expected_type = str(getattr(item, "param_type", "") or "") if item is not None else ""
    actual_type = _actual_export_param_type(input_socket, linked_node, store=store)
    param_type = expected_type or actual_type
    compact_type = _compact_param_type_label(param_type)
    node_type = str(getattr(linked_node, "type", "") or "")
    socket_type = str(getattr(input_socket, "type", "") or "")

    if compact_type in {"ITexture", "CTextureArray", "CCubeTexture"} or node_type in {'TEX_IMAGE', 'TEX_ENVIRONMENT', 'GROUP'}:
        return ('TEXTURE', "Textures", 'IMAGE_DATA', 3)
    if compact_type == "Float" or node_type == 'VALUE':
        return ('FLOAT', "Floats", 'DOT', 0)
    if compact_type == "Color" or node_type == 'RGB':
        return ('COLOR', "Colors", 'COLOR', 1)
    if compact_type == "Vector" or socket_type == 'VECTOR' or node_type in {'COMBXYZ', 'MAPPING'}:
        return ('VECTOR', "Vectors", 'EMPTY_ARROWS', 2)
    return ('OTHER', "Other", 'NODE', 4)


def _linked_primary_node_for_entry(node_ng, entry: dict):
    param_name = str(entry.get("name", "") or "")
    if not param_name:
        return None
    input_pin = find_group_input_socket(node_ng, param_name)
    if input_pin is None or not getattr(input_pin, "is_linked", False) or not input_pin.links:
        return None
    primary_link = input_pin.links[0]
    if primary_link is not None and not _linked_node_matches_chain_value(primary_link, entry):
        return None
    primary_node = getattr(primary_link, "from_node", None)
    if primary_node is None or not _node_has_material_chain_source(primary_node):
        return None
    return primary_node


def _layout_chain_nodes_by_source(material) -> int:
    props = getattr(material, "witcher_props", None)
    node_ng = get_active_witcher_group_node(material)
    if props is None or node_ng is None:
        return 0

    try:
        node_group_x = int(getattr(node_ng.location, "x", node_ng.location[0]))
    except Exception:
        node_group_x = 500

    entries = [
        _item_to_dict(item) for item in getattr(props, "base_read_params", []) or []
        if not bool(getattr(item, "is_declared_only", False))
    ]
    local_groups = _sync_local_override_nodes(material)
    source_metrics = _layout_source_metrics_for_entries(node_ng, entries)
    source_positions = _dynamic_source_x_positions(
        node_group_x,
        source_metrics.keys(),
        source_metrics,
        local_metrics=_local_source_metrics(local_groups) if local_groups else None,
    )

    moved = set()
    moved_count = 0
    target_y = CHAIN_NODE_ROW_Y
    source_gap = 110

    local_moved = 0
    for input_socket, primary_node in local_groups:
        local_moved += _move_upstream_group(
            primary_node,
            source_positions.get("local", local_node_x(node_group_x)),
            target_y,
            moved,
        )
        target_y -= _row_step_for_local_group(input_socket, primary_node)
    if local_moved:
        moved_count += local_moved
        target_y -= source_gap

    for chain_item in getattr(props, "base_read_chain", []) or []:
        source_index = coerce_source_index(getattr(chain_item, "source_index", -1))
        if source_index < 0:
            continue
        source_entries = [
            entry for entry in entries
            if _entry_matches_chain_item(entry, chain_item)
        ]
        if not source_entries:
            continue
        source_entries.sort(key=lambda entry: (
            coerce_source_index(entry.get("row_index")),
            str(entry.get("name", "") or "").lower(),
        ))

        source_moved = 0
        for entry in source_entries:
            primary_node = _linked_primary_node_for_entry(node_ng, entry)
            if primary_node is None:
                continue
            if bool(getattr(primary_node, "witcher_include", False)):
                continue
            source_moved += _move_upstream_group(
                primary_node,
                source_positions.get(source_index, chain_node_x(node_group_x, source_index)),
                target_y,
                moved,
            )
            target_y -= _row_step_for_entry_group(entry, primary_node)

        if source_moved:
            moved_count += source_moved
            target_y -= source_gap

    return moved_count


def _chain_frame_label(path: str, source_kind: str) -> str:
    label = _source_file_label(path) or _short_path_label(path, 36)
    source_kind = _source_kind_label(source_kind)
    return f"{source_kind}: {label}" if label else source_kind


def _find_chain_frame(material, source_path: str, source_index: int):
    target_key = _chain_source_key(source_path, source_index)
    for node in getattr(material.node_tree, "nodes", []) or []:
        if getattr(node, "type", "") != 'FRAME':
            continue
        try:
            if not bool(node.get("witcher_material_chain_frame")):
                continue
            node_key = _chain_source_key(
                str(node.get("witcher_material_source_path", "") or ""),
                node.get("witcher_material_source_index", -1),
            )
            if node_key == target_key:
                return node
        except Exception:
            continue
    return None


def _node_bounds(nodes) -> Optional[tuple]:
    bounds = None
    for node in nodes:
        if getattr(node, "type", "") == 'FRAME':
            continue
        x, y = _node_abs_location(node)
        width = float(getattr(node, "width", 200.0) or 200.0)
        height = float(getattr(node, "height", 140.0) or 140.0)
        node_bounds = (x, y - height, x + width, y)
        if bounds is None:
            bounds = node_bounds
        else:
            bounds = (
                min(bounds[0], node_bounds[0]),
                min(bounds[1], node_bounds[1]),
                max(bounds[2], node_bounds[2]),
                max(bounds[3], node_bounds[3]),
            )
    return bounds


def _find_local_frame(material):
    for node in getattr(material.node_tree, "nodes", []) or []:
        if getattr(node, "type", "") != 'FRAME':
            continue
        try:
            if bool(node.get("witcher_material_local_frame")):
                return node
        except Exception:
            continue
    return None


def _apply_local_frame(material, *, create_missing: bool = True) -> int:
    nodes = [
        node for node in _iter_local_nodes(material, linked_only=True) or []
        if getattr(node, "type", "") != 'FRAME'
    ]
    frame = _find_local_frame(material)
    if not nodes:
        if frame is not None:
            try:
                material.node_tree.nodes.remove(frame)
            except Exception:
                pass
        return 0
    if frame is None:
        if not create_missing:
            return 0
        frame = material.node_tree.nodes.new(type='NodeFrame')

    frame.name = "Witcher Chain - Local"
    frame.label = "Local"
    frame["witcher_material_chain_frame"] = True
    frame["witcher_material_local_frame"] = True
    try:
        frame.use_custom_color = True
        frame.color = _local_node_color(material)
    except Exception:
        pass

    for node in nodes:
        _set_node_parent_keep_location(node, None)
    bounds = _node_bounds(nodes)
    if bounds is None:
        return 0
    min_x, min_y, max_x, max_y = bounds
    margin_x = 70
    margin_y = 55
    frame.location.x = min_x - margin_x
    frame.location.y = max_y + margin_y
    frame.width = max(260, (max_x - min_x) + (margin_x * 2))
    frame.height = max(180, (max_y - min_y) + (margin_y * 2))
    framed_count = 0
    for node in nodes:
        _set_node_parent_keep_location(node, frame)
        framed_count += 1
    return framed_count


def _apply_chain_frames(material, *, create_missing: bool = True) -> int:
    props = getattr(material, "witcher_props", None)
    if props is None or getattr(material, "node_tree", None) is None:
        return 0

    framed_count = _apply_local_frame(material, create_missing=create_missing)
    margin_x = 70
    margin_y = 55
    for chain_item in getattr(props, "base_read_chain", []) or []:
        source_path = str(getattr(chain_item, "path", "") or "")
        source_index = coerce_source_index(getattr(chain_item, "source_index", -1))
        nodes = [
            node for node in _iter_chain_source_nodes(material, source_path, source_index, linked_only=True) or []
            if getattr(node, "type", "") != 'FRAME'
        ]
        if not nodes:
            continue

        frame = _find_chain_frame(material, source_path, source_index)
        if frame is None:
            if not create_missing:
                continue
            frame = material.node_tree.nodes.new(type='NodeFrame')
        label = _chain_frame_label(source_path, getattr(chain_item, "source_kind", ""))
        frame.name = f"Witcher Chain - {label}"
        frame.label = label
        frame["witcher_material_chain_frame"] = True
        frame["witcher_material_source_path"] = source_path
        frame["witcher_material_source_index"] = source_index
        try:
            frame.use_custom_color = True
            frame.color = tuple(float(value) for value in getattr(chain_item, "node_color", (0.5, 0.5, 0.5))[:3])
        except Exception:
            pass

        for node in nodes:
            _set_node_parent_keep_location(node, None)
        bounds = _node_bounds(nodes)
        if bounds is None:
            continue
        min_x, min_y, max_x, max_y = bounds
        frame.location.x = min_x - margin_x
        frame.location.y = max_y + margin_y
        frame.width = max(260, (max_x - min_x) + (margin_x * 2))
        frame.height = max(180, (max_y - min_y) + (margin_y * 2))
        for node in nodes:
            _set_node_parent_keep_location(node, frame)
            framed_count += 1

    return framed_count


def _remove_chain_frames(material) -> int:
    if material is None or getattr(material, "node_tree", None) is None:
        return 0
    nodes = material.node_tree.nodes
    frames = [
        node for node in nodes
        if getattr(node, "type", "") == 'FRAME' and bool(node.get("witcher_material_chain_frame"))
    ]
    removed_count = 0
    for frame in frames:
        children = [
            node for node in nodes
            if getattr(node, "parent", None) == frame
        ]
        for child in children:
            _set_node_parent_keep_location(child, None)
        try:
            nodes.remove(frame)
            removed_count += 1
        except Exception:
            continue
    return removed_count


def _set_base_read_snapshot(material, inspection: dict, *, status: str = "ok", message: str = "", count_created: int = 0):
    props = material.witcher_props
    existing_chain_colors = _capture_chain_item_colors(props)
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
        item.source_index = coerce_source_index(entry.get("source_index"))
        item.row_index = coerce_source_index(entry.get("row_index"))
        item.row_y = coerce_source_index(entry.get("row_y"))
        item.has_value = bool(entry.get("has_value", False))
        item.has_matching_socket = bool(entry.get("has_matching_socket", False))
        item.is_linked = bool(entry.get("is_linked", False))
        item.is_supported = bool(entry.get("is_supported", False))
        item.is_declared_only = bool(entry.get("is_declared_only", False))
        item.can_create = bool(entry.get("can_create", False))
        item.status = str(entry.get("status", "") or "")
        item.message = str(entry.get("message", "") or "")

    props.base_read_chain.clear()
    for entry in inspection.get("chain", []) or []:
        chain_item = props.base_read_chain.add()
        chain_item.path = str(entry.get("path", "") or "")
        chain_item.source_kind = str(entry.get("source_kind", "") or "")
        chain_item.chunk_type = str(entry.get("chunk_type", "") or "")
        chain_item.source_index = coerce_source_index(entry.get("source_index"))
        key = _chain_item_color_key(chain_item.path, chain_item.source_index)
        color = existing_chain_colors.get(key)
        if color is None:
            color = chain_color_for_index(chain_item.source_index) or (0.5, 0.5, 0.5)
        _set_chain_item_node_color(chain_item, color)

    _tag_existing_linked_chain_nodes(material, inspection)
    _layout_chain_nodes_by_source(material)
    _apply_chain_item_colors_to_nodes(material)
    if bool(getattr(props, "base_read_chain_frames_enabled", True)):
        _apply_chain_frames(material, create_missing=True)
    else:
        _remove_chain_frames(material)
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


def _find_base_read_item_for_socket(mat_props, node_ng, input_socket):
    if mat_props is None or node_ng is None or input_socket is None:
        return None
    target_socket_id = None
    try:
        target_socket_id = input_socket.as_pointer()
    except Exception:
        pass
    for item in getattr(mat_props, "base_read_params", []) or []:
        item_socket = find_group_input_socket(node_ng, str(getattr(item, "name", "") or ""))
        if item_socket is None:
            continue
        if item_socket is input_socket:
            return item
        if target_socket_id is not None:
            try:
                if item_socket.as_pointer() == target_socket_id:
                    return item
            except Exception:
                pass
    return None


def _linked_primary_for_param_name(material, param_name: str):
    node_ng = get_active_witcher_group_node(material)
    if node_ng is None:
        return None, None
    input_pin = find_group_input_socket(node_ng, param_name)
    if input_pin is None or not getattr(input_pin, "is_linked", False) or not input_pin.links:
        return input_pin, None
    return input_pin, getattr(input_pin.links[0], "from_node", None)


def _linked_item_uses_user_node(material, item) -> bool:
    if not bool(getattr(item, "is_linked", False)):
        return False
    _, primary_node = _linked_primary_for_param_name(material, str(getattr(item, "name", "") or ""))
    return _is_user_created_linked_node(primary_node)


def _is_param_promoted_to_local(material, param_name: str) -> bool:
    _, primary_node = _linked_primary_for_param_name(material, param_name)
    return bool(primary_node is not None and getattr(primary_node, "witcher_include", False))


def is_node_export_enabled(node) -> bool:
    if node is None:
        return False
    return bool(getattr(node, "witcher_export", True))


_AUXILIARY_TEXTURE_ALPHA_LINKS = {
    "Roughness": "Normal",
    "Alpha": "Diffuse",
}


def _node_material_param_name(node) -> str:
    if node is None:
        return ""
    for prop_name in ("witcher_material_source_param", "witcher_param_name"):
        value = _node_string_prop(node, prop_name)
        if value:
            return _NUMERIC_SUFFIX_RE.sub("", value.strip())
    for attr_name in ("name", "label"):
        value = str(getattr(node, attr_name, "") or "").strip()
        if value:
            return _NUMERIC_SUFFIX_RE.sub("", value)
    return ""


def _linked_node_feeds_group_input(input_socket, linked_node, param_name: str) -> bool:
    group_node = getattr(input_socket, "node", None)
    target_socket = find_group_input_socket(group_node, param_name) if group_node is not None else None
    if target_socket is None or not getattr(target_socket, "is_linked", False):
        return False
    for link in getattr(target_socket, "links", []) or []:
        if getattr(link, "from_node", None) is linked_node:
            return True
    return False


def is_auxiliary_material_display_link(input_socket, linked_socket=None, linked_node=None) -> bool:
    """Return True for Blender preview links that should not become export params."""
    if input_socket is None:
        return False
    if linked_socket is None:
        links = getattr(input_socket, "links", None) or []
        linked_socket = getattr(links[0], "from_socket", None) if links else None
    if linked_node is None and linked_socket is not None:
        linked_node = getattr(linked_socket, "node", None)
    if linked_node is None or getattr(linked_node, "type", "") != 'TEX_IMAGE':
        return False

    linked_socket_name = str(getattr(linked_socket, "name", "") or getattr(linked_socket, "identifier", "") or "")
    if linked_socket_name != "Alpha":
        return False

    input_name = str(getattr(input_socket, "name", "") or "")
    source_param = _AUXILIARY_TEXTURE_ALPHA_LINKS.get(input_name)
    if not source_param:
        return False

    node_param = _node_material_param_name(linked_node)
    return node_param == source_param or _linked_node_feeds_group_input(input_socket, linked_node, source_param)


def _expected_export_param_type(mat_props, node_ng, input_socket, linked_node) -> str:
    item = _find_base_read_item_for_socket(mat_props, node_ng, input_socket)
    if item is not None:
        param_type = str(getattr(item, "param_type", "") or "")
        if param_type:
            return param_type
    for prop_name in ("witcher_material_source_type", "witcher_material_param_type"):
        value = _node_string_prop(linked_node, prop_name)
        if value:
            return value
    return ""


def _actual_export_param_type(input_socket, linked_node, *, store: bool = True) -> str:
    node_type = str(getattr(linked_node, "type", "") or "")
    if node_type == 'GROUP':
        if get_texarray_group_value(linked_node, store=store):
            return "handle:CTextureArray"
        return "GROUP"
    if node_type == 'TEX_IMAGE':
        return "handle:ITexture"
    if node_type == 'TEX_ENVIRONMENT':
        return "handle:CCubeTexture"
    if node_type == 'RGB':
        return "Color"
    if node_type == 'VALUE':
        return "Float"
    if node_type in {'COMBXYZ', 'MAPPING'} or str(getattr(input_socket, "type", "") or "") == 'VECTOR':
        return "Vector"
    return node_type


def _export_param_type_matches(expected: str, actual: str) -> bool:
    expected = str(expected or "")
    actual = str(actual or "")
    if not expected:
        return True
    if expected == actual:
        return True
    if expected == "handle:ITexture":
        return actual == "handle:ITexture"
    if expected == "handle:CCubeTexture":
        return actual == "handle:CCubeTexture"
    if expected == "handle:CTextureArray":
        return actual == "handle:CTextureArray"
    if expected == "Float":
        return actual == "Float"
    if expected == "Color":
        return actual == "Color"
    if expected == "Vector":
        return actual == "Vector"
    return False


def _linked_socket_type_validation_issue(material, input_socket, primary_node, *, store: bool = True) -> str:
    if material is None or input_socket is None or primary_node is None:
        return ""
    if is_auxiliary_material_display_link(input_socket, linked_node=primary_node):
        return ""
    props = getattr(material, "witcher_props", None)
    node_ng = get_active_witcher_group_node(material)
    expected_type = _expected_export_param_type(props, node_ng, input_socket, primary_node)
    actual_type = _actual_export_param_type(input_socket, primary_node, store=store)
    if not _export_param_type_matches(expected_type, actual_type):
        return f"{input_socket.name}: expected {expected_type or 'unknown'}, got {actual_type or 'unknown'}"
    return ""


def _linked_item_type_validation_issue(material, item, *, store: bool = True) -> str:
    input_socket, primary_node = _linked_primary_for_param_name(material, str(getattr(item, "name", "") or ""))
    return _linked_socket_type_validation_issue(material, input_socket, primary_node, store=store)


def validate_material_export_params(material) -> list[str]:
    issues = []
    if material is None or getattr(material, "witcher_props", None) is None:
        return ["No Witcher material selected."]
    node_ng = get_active_witcher_group_node(material)
    if node_ng is None:
        return []

    props = material.witcher_props
    for input_socket in getattr(node_ng, "inputs", []) or []:
        if not getattr(input_socket, "is_linked", False) or not getattr(input_socket, "links", None):
            continue
        linked_socket = input_socket.links[0].from_socket
        linked_node = linked_socket.node
        if is_auxiliary_material_display_link(input_socket, linked_socket, linked_node):
            continue
        if not bool(getattr(linked_node, "witcher_include", False)):
            continue
        if not is_node_export_enabled(linked_node):
            continue

        expected_type = _expected_export_param_type(props, node_ng, input_socket, linked_node)
        actual_type = _actual_export_param_type(input_socket, linked_node)
        if not _export_param_type_matches(expected_type, actual_type):
            issues.append(
                f"{input_socket.name}: expected {expected_type or 'unknown'}, got {actual_type or 'unknown'}"
            )
            continue

        effective_type = expected_type or actual_type
        if effective_type in {"handle:ITexture", "handle:CTextureArray", "handle:CCubeTexture"}:
            value = get_texarray_group_value(linked_node) if linked_node.type == 'GROUP' else get_socket_value(input_socket)
            if not isinstance(value, str) or not value.strip():
                issues.append(f"{input_socket.name}: missing texture path for {effective_type}")
    return issues


def _is_base_read_auto_create_entry(entry) -> bool:
    if not bool(getattr(entry, "can_create", False)):
        return False
    if bool(getattr(entry, "is_linked", False)):
        return False
    status = str(getattr(entry, "status", "") or "")
    if status == "available_to_create":
        return True
    return bool(
        status == "unsupported_export_only"
        and str(getattr(entry, "source_kind", "") or "") == "instance"
        and bool(getattr(entry, "has_value", False))
    )


def _is_base_read_auto_create_dict(entry: dict) -> bool:
    if not bool(entry.get("can_create", False)):
        return False
    if bool(entry.get("is_linked", False)):
        return False
    status = str(entry.get("status", "") or "")
    if status == "available_to_create":
        return True
    return bool(
        status == "unsupported_export_only"
        and str(entry.get("source_kind", "") or "") == "instance"
        and bool(entry.get("has_value", False))
    )


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


def _unlink_input_socket_links(material, input_socket) -> int:
    if material is None or input_socket is None or getattr(material, "node_tree", None) is None:
        return 0
    removed = 0
    for link in list(getattr(input_socket, "links", []) or []):
        try:
            material.node_tree.links.remove(link)
            removed += 1
        except Exception:
            continue
    return removed


def _remove_unshared_upstream_nodes(material, upstream_nodes) -> int:
    if material is None or getattr(material, "node_tree", None) is None:
        return 0
    candidates = [
        node for node in upstream_nodes
        if node is not None and getattr(node, "type", "") != 'FRAME'
    ]
    if not candidates:
        return 0

    node_by_id = {_node_identity(node): node for node in candidates}
    removable_ids = set(node_by_id.keys())
    changed = True
    while changed:
        changed = False
        for node_id, node in list(node_by_id.items()):
            if node_id not in removable_ids:
                continue
            for output_socket in getattr(node, "outputs", []) or []:
                for link in getattr(output_socket, "links", []) or []:
                    linked_node = getattr(link, "to_node", None)
                    if linked_node is None:
                        continue
                    if _node_identity(linked_node) not in removable_ids:
                        removable_ids.remove(node_id)
                        changed = True
                        break
                if node_id not in removable_ids:
                    break

    removed = 0
    for node in reversed(candidates):
        if _node_identity(node) not in removable_ids:
            continue
        try:
            material.node_tree.nodes.remove(node)
            removed += 1
        except Exception:
            continue
    return removed


def _node_feeds_other_group_input(material, primary_node, input_socket) -> bool:
    node_ng = get_active_witcher_group_node(material)
    if node_ng is None or primary_node is None:
        return False
    input_socket_id = _node_identity(input_socket)
    primary_node_id = _node_identity(primary_node)
    for candidate_socket in getattr(node_ng, "inputs", []) or []:
        if _node_identity(candidate_socket) == input_socket_id:
            continue
        for link in getattr(candidate_socket, "links", []) or []:
            if _node_identity(getattr(link, "from_node", None)) == primary_node_id:
                return True
    return False


def _remove_user_linked_param_graph(material, input_socket, primary_node) -> tuple[int, int]:
    upstream_nodes = list(_collect_upstream_nodes(primary_node)) if primary_node is not None else []
    feeds_other_group_input = _node_feeds_other_group_input(material, primary_node, input_socket)
    unlinked_count = _unlink_input_socket_links(material, input_socket)
    try:
        if primary_node is not None and primary_node.get("witcher_material_user_local") is not None:
            del primary_node["witcher_material_user_local"]
        if primary_node is not None and not feeds_other_group_input:
            primary_node.witcher_include = False
    except Exception:
        pass
    _sync_local_override_nodes(material)
    removed_count = _remove_unshared_upstream_nodes(material, upstream_nodes)
    return unlinked_count, removed_count


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
    source_index: bpy.props.IntProperty(name="Source Index", default=-1)
    row_index: bpy.props.IntProperty(name="Row Index", default=-1)
    row_y: bpy.props.IntProperty(name="Row Y", default=-1)
    has_value: bpy.props.BoolProperty(name="Has Value", default=False)
    has_matching_socket: bpy.props.BoolProperty(name="Has Matching Socket", default=False)
    is_linked: bpy.props.BoolProperty(name="Is Linked", default=False)
    is_supported: bpy.props.BoolProperty(name="Is Supported", default=False)
    is_declared_only: bpy.props.BoolProperty(name="Is Declared Only", default=False)
    can_create: bpy.props.BoolProperty(name="Can Create", default=False)
    status: bpy.props.StringProperty(name="Status")
    message: bpy.props.StringProperty(name="Message")
    show_details: bpy.props.BoolProperty(name="Show Details", default=False)


class BaseMaterialChainItem(bpy.types.PropertyGroup):
    path: bpy.props.StringProperty(name="Path")
    source_kind: bpy.props.StringProperty(name="Source Kind")
    chunk_type: bpy.props.StringProperty(name="Chunk Type")
    source_index: bpy.props.IntProperty(name="Source Index", default=-1)
    node_color: bpy.props.FloatVectorProperty(
        name="Node Color",
        description="Color used by nodes created from this material-chain entry",
        subtype='COLOR',
        size=3,
        min=0.0,
        max=1.0,
        default=(0.5, 0.5, 0.5),
        update=_update_base_material_chain_color,
    )


def _update_material_version(self, context):
    current_base = normalize_depot_path(getattr(self, "base_custom", ""))
    if self.material_version == "witcher2" and current_base == normalize_depot_path(DEFAULT_W3_MATERIAL_BASE):
        self.base_custom = DEFAULT_W2_MATERIAL_BASE
    elif self.material_version == "witcher3" and current_base == normalize_depot_path(DEFAULT_W2_MATERIAL_BASE):
        self.base_custom = DEFAULT_W3_MATERIAL_BASE


class WitcherMaterialProperties(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty(name="name", default="Material")
    enableMask: bpy.props.BoolProperty(name="enableMask", default=False, description="Enable Mask of hair etc")
    local: bpy.props.BoolProperty(name="local", default=True, description="Local materials will be embedded in the .w2mesh. Non-local will use the defined base material without any instances.")
    material_ui_tab: bpy.props.EnumProperty(
        name="Material UI Tab",
        items=[
            ('EXPORT', "Export Params", "Export-connected local params"),
            ('BASE', "Material Chain", "Read the material chain and create params from it"),
        ],
        default='EXPORT',
    )
    export_params_sort_mode: bpy.props.EnumProperty(
        name="Export Sort",
        description="Sort the Export Params list",
        items=EXPORT_PARAMS_SORT_MODE_ITEMS,
        default='TYPE',
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
    base_read_chain: bpy.props.CollectionProperty(type=BaseMaterialChainItem)
    base_read_params: bpy.props.CollectionProperty(type=BaseMaterialParamItem)
    base_read_chain_frames_enabled: bpy.props.BoolProperty(name="Frame Chain Nodes", default=True)
    base_read_value_search: bpy.props.StringProperty(
        name="Search Values",
        description="Filter Material Chain values by name, source path, value, or status",
        default="",
    )
    base_read_value_type_filter: bpy.props.EnumProperty(
        name="Type",
        description="Filter Material Chain values by parameter type",
        items=BASE_READ_VALUE_TYPE_FILTER_ITEMS,
        default='ALL',
    )
    base_read_local_color: bpy.props.FloatVectorProperty(
        name="Local Color",
        description="Color used by nodes promoted to local material overrides",
        subtype='COLOR',
        size=3,
        min=0.0,
        max=1.0,
        default=LOCAL_NODE_COLOR,
        update=_update_base_material_local_color,
    )
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
        default=DEFAULT_W3_MATERIAL_BASE,
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
        update=_update_material_version,
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

from .CR2W.common_blender import repo_file, win_safe_path, win_unprefix_path


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
        root = str(root or "").strip()
        if not root:
            return None
        root = win_unprefix_path(os.path.realpath(bpy.path.abspath(root))).rstrip("\\/")
        if not root or not os.path.isdir(win_safe_path(root)):
            return None
        try:
            root_key = os.path.normcase(os.path.normpath(root))
            path_key = os.path.normcase(os.path.normpath(path))
            if os.path.commonpath([root_key, path_key]) != root_key:
                return None
            rel_path = os.path.relpath(path, root)
        except Exception:
            return None
        if not rel_path or rel_path == os.curdir or rel_path.startswith("..") or os.path.isabs(rel_path):
            return None
        return rel_path

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

    # Witcher 2 roots. These need to be checked independently of the W3
    # texture/uncook roots so N-panel texture paths remain depot-relative.
    for root in w2_source_roots(bpy.context):
        result = _try_strip_root(texture_path, root)
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

    source_root = w2_source_repo_root_if_configured(texture_path)
    result = _try_strip_root(texture_path, source_root)
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

def _texarray_source_from_value(path: str) -> str:
    normalized = normalize_depot_path(str(path or "").strip().strip('"'))
    if not normalized:
        return ""
    if normalized.lower().endswith(".texarray"):
        return normalized
    match = _TEXARRAY_SLICE_RE.match(normalized)
    return match.group(1) if match else ""


def _node_string_prop(node, prop_name: str) -> str:
    if node is None:
        return ""
    try:
        value = getattr(node, prop_name, "")
        if value:
            return str(value)
    except Exception:
        pass
    try:
        value = node.get(prop_name, "")
        if value:
            return str(value)
    except Exception:
        pass
    return ""


def _node_bool_prop(node, prop_name: str, default: bool = False) -> bool:
    if node is None:
        return default
    try:
        return bool(getattr(node, prop_name))
    except Exception:
        pass
    try:
        return bool(node.get(prop_name, default))
    except Exception:
        return default


def _set_node_string_prop(node, prop_name: str, value: str) -> str:
    if node is None:
        return ""
    value = str(value or "")
    try:
        node[prop_name] = value
    except Exception:
        pass
    try:
        setattr(node, prop_name, value)
    except Exception:
        pass
    return value


def _normalize_texture_repo_path(path: str) -> str:
    path = str(path or "").strip().strip('"')
    if not path:
        return ""
    texarray_source = _texarray_source_from_value(path)
    if texarray_source:
        return texarray_source
    if os.path.isabs(path):
        path = get_repo_from_abs_path(path)
    return normalize_depot_path(path).lstrip("\\/")


def _set_texarray_group_source_path(group_node, source_path: str) -> str:
    source_path = _texarray_source_from_value(source_path)
    if not source_path:
        return ""
    try:
        group_node["witcher_texarray_source_path"] = source_path
    except Exception:
        pass
    try:
        group_node.witcher_texarray_source_path = source_path
    except Exception:
        pass
    return source_path


def _set_texture_node_source_path(texture_node, source_path: str) -> str:
    source_path = _normalize_texture_repo_path(source_path)
    if not source_path:
        return ""
    return _set_node_string_prop(texture_node, "witcher_texture_source_path", source_path)


def _texture_node_auto_repo_path(texture_node) -> str:
    if texture_node is None or getattr(texture_node, "type", "") not in {'TEX_IMAGE', 'TEX_ENVIRONMENT'}:
        return ""
    image = getattr(texture_node, "image", None)
    if image is None:
        return ""
    rel_path = win_unprefix_path(getattr(image, "filepath", "") or "")
    if not rel_path:
        return ""
    try:
        abs_path = win_unprefix_path(bpy.path.abspath(rel_path))
        extension = '.w2cube' if getattr(texture_node, "type", "") == 'TEX_ENVIRONMENT' else '.xbm'
        repo_path = get_repo_from_abs_path(os.path.normpath(abs_path), extension=extension)
        texarray_source = _texarray_source_from_value(repo_path)
        return texarray_source or _normalize_texture_repo_path(repo_path)
    except Exception:
        return ""


def _auto_resolve_texture_node_source_path(texture_node, *, force: bool = False, store: bool = True) -> str:
    if texture_node is None:
        return ""
    existing = _normalize_texture_repo_path(_node_string_prop(texture_node, "witcher_texture_source_path"))
    if existing and _node_bool_prop(texture_node, "witcher_texture_path_manual") and not force:
        return existing
    auto_path = _texture_node_auto_repo_path(texture_node)
    if auto_path:
        return _set_texture_node_source_path(texture_node, auto_path) if store else auto_path
    if existing:
        return _set_texture_node_source_path(texture_node, existing) if store else existing
    return ""


def _texture_node_export_path(texture_node, material=None, *, store: bool = True) -> str:
    manual_path = _normalize_texture_repo_path(_node_string_prop(texture_node, "witcher_texture_source_path"))
    manual_enabled = _node_bool_prop(texture_node, "witcher_texture_path_manual")
    if manual_enabled and manual_path:
        return _set_texture_node_source_path(texture_node, manual_path) if store else manual_path
    repo_path = _auto_resolve_texture_node_source_path(texture_node, store=store)
    if not repo_path:
        return ""
    props = getattr(material, "witcher_props", None)
    if (
        props is not None
        and bool(getattr(props, "override_texture_root", False))
        and not _texarray_source_from_value(repo_path)
    ):
        return str(getattr(props, "custom_texture_root", "") or "") + os.path.basename(repo_path)
    return repo_path


def _texture_node_texarray_source_path(texture_node) -> str:
    if texture_node is None or getattr(texture_node, "type", "") != 'TEX_IMAGE':
        return ""
    for prop_name in ("witcher_texture_source_path", "witcher_texarray_source_path"):
        source_path = _texarray_source_from_value(_node_string_prop(texture_node, prop_name))
        if source_path:
            return _set_texture_node_source_path(texture_node, source_path)

    image = getattr(texture_node, "image", None)
    if image is None:
        return ""
    try:
        rel_path = win_unprefix_path(getattr(image, "filepath", "") or "")
        abs_path = win_unprefix_path(bpy.path.abspath(rel_path))
        source_path = _texarray_source_from_value(get_repo_from_abs_path(os.path.normpath(abs_path)))
        if source_path:
            return _set_texture_node_source_path(texture_node, source_path)
    except Exception:
        pass
    return ""


def get_texarray_texture_value(texture_node) -> str:
    return _texture_node_texarray_source_path(texture_node)


def _is_texarray_group_node(group_node) -> bool:
    if group_node is None or getattr(group_node, "type", "") != 'GROUP':
        return False
    param_type = _node_string_prop(group_node, "witcher_material_param_type")
    if param_type == "handle:CTextureArray":
        return True
    if _texarray_source_from_value(_node_string_prop(group_node, "witcher_texarray_source_path")):
        return True
    node_tree = getattr(group_node, "node_tree", None)
    node_tree_name = str(getattr(node_tree, "name", "") or "").lower()
    return "witchertexarray" in node_tree_name


def _texarray_group_source_path(group_node, *, store: bool = True) -> str:
    if not _is_texarray_group_node(group_node):
        return ""
    if _node_bool_prop(group_node, "witcher_texarray_path_manual"):
        manual_source = _texarray_source_from_value(_node_string_prop(group_node, "witcher_texarray_source_path"))
        if manual_source:
            return _set_texarray_group_source_path(group_node, manual_source) if store else manual_source
    auto_source = _auto_resolve_texarray_group_source_path(group_node, force=True, store=store)
    if auto_source:
        return auto_source
    for prop_name in ("witcher_texarray_source_path", "witcher_material_texarray_source_path"):
        try:
            source_path = _texarray_source_from_value(_node_string_prop(group_node, prop_name))
            if source_path:
                return _set_texarray_group_source_path(group_node, source_path) if store else source_path
        except Exception:
            pass

    for input_socket in getattr(group_node, "inputs", []) or []:
        if not getattr(input_socket, "is_linked", False) or not getattr(input_socket, "links", None):
            continue
        linked_node = getattr(input_socket.links[0].from_socket, "node", None)
        if linked_node is None or getattr(linked_node, "type", "") != 'TEX_IMAGE':
            continue
        image = getattr(linked_node, "image", None)
        if image is None:
            continue
        try:
            rel_path = win_unprefix_path(getattr(image, "filepath", "") or "")
            abs_path = win_unprefix_path(bpy.path.abspath(rel_path))
            source_path = _texarray_source_from_value(get_repo_from_abs_path(os.path.normpath(abs_path)))
            if source_path:
                return _set_texarray_group_source_path(group_node, source_path) if store else source_path
        except Exception:
            continue
    return ""


def _auto_resolve_texarray_group_source_path(group_node, *, force: bool = False, store: bool = True) -> str:
    if not _is_texarray_group_node(group_node):
        return ""
    existing = _texarray_source_from_value(_node_string_prop(group_node, "witcher_texarray_source_path"))
    if existing and _node_bool_prop(group_node, "witcher_texarray_path_manual") and not force:
        return _set_texarray_group_source_path(group_node, existing) if store else existing

    for input_socket in getattr(group_node, "inputs", []) or []:
        if not getattr(input_socket, "is_linked", False) or not getattr(input_socket, "links", None):
            continue
        linked_node = getattr(input_socket.links[0].from_socket, "node", None)
        if linked_node is None or getattr(linked_node, "type", "") != 'TEX_IMAGE':
            continue
        source_path = _texarray_source_from_value(_texture_node_auto_repo_path(linked_node))
        if source_path:
            return _set_texarray_group_source_path(group_node, source_path) if store else source_path

    if existing:
        return _set_texarray_group_source_path(group_node, existing) if store else existing
    return ""


def get_texarray_group_value(group_node, *, store: bool = True) -> str:
    return _texarray_group_source_path(group_node, store=store)


def get_socket_value(input_socket):
    if input_socket.is_linked:
        linked_socket = input_socket.links[0].from_socket
        if linked_socket.node.type == 'GROUP':
            texarray_source_path = get_texarray_group_value(linked_socket.node)
            if texarray_source_path:
                return texarray_source_path
        if linked_socket.node.type == 'TEX_IMAGE':
            texarray_source_path = get_texarray_texture_value(linked_socket.node)
            if texarray_source_path:
                return texarray_source_path
            mat = next((m for m in bpy.data.materials if m.node_tree == input_socket.node.id_data and hasattr(m, 'witcher_props')), None)
            export_path = _texture_node_export_path(linked_socket.node, mat)
            if export_path:
                return export_path
        elif linked_socket.node.type == 'TEX_ENVIRONMENT':
            return _texture_node_export_path(linked_socket.node)
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


def _existing_disk_path(path: str) -> str:
    path = str(path or "").strip()
    if not path:
        return ""
    disk_path = win_unprefix_path(os.path.normpath(path))
    try:
        if os.path.exists(win_safe_path(disk_path)):
            return disk_path
    except Exception:
        pass
    return ""


def _source_location_repo_candidates(source_path: str) -> list[str]:
    source_path = str(source_path or "").strip().strip('"')
    if not source_path or os.path.isabs(source_path):
        return []

    normalized = normalize_depot_path(source_path).lstrip("\\/")
    candidates = []

    def add(candidate: str) -> None:
        candidate = normalize_depot_path(candidate).lstrip("\\/")
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    texarray_source = _texarray_source_from_value(normalized)
    if texarray_source:
        add(texarray_source)
        if normalized != texarray_source:
            add(normalized)
        for ext in _TEXTURE_LOCATION_EXTS:
            add(f"{texarray_source}.texture_0{ext}")
        return candidates

    add(normalized)
    root, ext = os.path.splitext(normalized)
    ext = ext.lower()
    if ext in _TEXTURE_LOCATION_EXTS:
        for alt_ext in _TEXTURE_LOCATION_EXTS:
            add(root + alt_ext)
    return candidates


def _source_location_roots(context) -> list[str]:
    roots = []

    def add(root: str) -> None:
        root = win_unprefix_path(str(root or "").strip())
        if root and root not in roots:
            roots.append(root)

    try:
        add(get_texture_path(context))
        add(get_uncook_path(context))
        add(get_modded_texture_path(context))
        mod_dir = get_mod_directory(context)
        add(mod_dir)
        if mod_dir:
            for folder in possible_folders:
                add(os.path.join(mod_dir, folder))
    except Exception:
        pass

    try:
        prefs = get_all_addon_prefs(context)
        for path_item in getattr(prefs, "path_list", []) or []:
            add(getattr(path_item, "path", ""))
        for path_item in getattr(prefs, "redkit_projects", []) or []:
            project_path = getattr(path_item, "path", "")
            if project_path:
                add(os.path.join(project_path, "workspace"))
                add(project_path)
        add(getattr(prefs, "redkit_depot_path", ""))
        add(getattr(prefs, "redkit_uncooked_path", ""))
    except Exception:
        pass

    return roots


def _resolve_source_location_path(context, source_path: str, repo_version: int) -> str:
    source_path = str(source_path or "").strip().strip('"')
    if not source_path:
        return ""

    disk_path = _existing_disk_path(source_path)
    if disk_path:
        return disk_path

    repo_candidates = _source_location_repo_candidates(source_path)
    for candidate in repo_candidates:
        try:
            disk_path = _existing_disk_path(repo_file(candidate, version=repo_version))
            if disk_path:
                return disk_path
        except Exception:
            pass

    for root in _source_location_roots(context):
        for candidate in repo_candidates:
            disk_path = _existing_disk_path(os.path.join(root, candidate))
            if disk_path:
                return disk_path

    return ""


def _refresh_base_read_snapshot(material, material_path: str, *, count_created: int = 0, status: str = "ok", message: str = "") -> dict:
    inspection = inspect_material_base_path(material, material_path)
    if inspection.get("errors"):
        status = "error"
        if not message:
            message = str(inspection["errors"][0])
    _set_base_read_snapshot(material, inspection, status=status, message=message, count_created=count_created)
    return inspection


def auto_load_base_material_snapshot(context, material, *, create_missing: bool = True) -> dict:
    if material is None or getattr(material, "witcher_props", None) is None:
        return {}

    material_path = getattr(material.witcher_props, "base_custom", "")
    inspection = inspect_material_base_path(material, material_path)
    if inspection.get("errors"):
        message = str(inspection["errors"][0])
        _set_base_read_snapshot(material, inspection, status="error", message=message, count_created=0)
        return inspection

    created = 0
    reused = 0
    if create_missing and inspection.get("has_active_witcher_group"):
        entries = [
            entry for entry in inspection.get("inventory", []) or []
            if _is_base_read_auto_create_dict(entry)
        ]
        created, reused = _apply_base_read_entries(context, material, entries, allow_export_socket=True)
        if created or reused:
            inspection = inspect_material_base_path(material, material_path)

    message = f"Loaded Base Path snapshot on import. Created {created} helper node(s)"
    if reused:
        message += f", reused {reused}"
    _set_base_read_snapshot(
        material,
        inspection,
        status="ok",
        message=message,
        count_created=created,
    )
    return inspection


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

__classes = [
    ClearInputPropsOperator,
    WITCH_OT_material_chain_help,
    WITCH_OT_validate_material_export_params,
    WITCH_OT_autoresolve_texture_repo_path,
    WITCH_OT_search_base_material_path,
    WITCH_OT_use_recommended_base_material_group,
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
    WITCH_UL_base_material_paths,
    WITCH_PT_materials,
    ReplacePrincipledBSDFOperator,
    WITCH_OT_copy_texture_path,
]

def register():
    bpy.types.Node.witcher_include = bpy.props.BoolProperty(
        name="Local",
        description="Keep this linked node as a local material override",
        default=False,
        update=_update_node_witcher_include,
    )
    bpy.types.Node.witcher_export = bpy.props.BoolProperty(
        name="Export",
        description="Write this Local material parameter into the exported mesh",
        default=True,
    )
    bpy.types.Node.witcher_final_path = bpy.props.StringProperty(default="")
    bpy.types.Node.witcher_param_kind = bpy.props.StringProperty(default="")
    bpy.types.Node.witcher_param_name = bpy.props.StringProperty(default="")
    bpy.types.Node.witcher_vector_source = bpy.props.StringProperty(default="")
    bpy.types.Node.witcher_vector_w = bpy.props.FloatProperty(default=1.0)
    bpy.types.Node.witcher_texarray_source_path = bpy.props.StringProperty(
        name="Texarray Path",
        description="Repo path exported for CTextureArray nodes. Keep the .texarray path, not a generated texture slice",
        default="",
    )
    bpy.types.Node.witcher_texarray_path_manual = bpy.props.BoolProperty(
        name="Manual Texarray Path",
        description="Edit the CTextureArray repo path manually instead of using auto-resolve",
        default=False,
    )
    bpy.types.Node.witcher_texture_source_path = bpy.props.StringProperty(
        name="Texture Path",
        description="Repo path exported for texture nodes that need an explicit source path",
        default="",
    )
    bpy.types.Node.witcher_texture_path_manual = bpy.props.BoolProperty(
        name="Manual Texture Path",
        description="Edit the texture repo path manually instead of using auto-resolve",
        default=False,
    )
    bpy.utils.register_class(NodeGroupInputProperties) #! imp to reg first
    bpy.utils.register_class(BaseMaterialPathItem)
    bpy.utils.register_class(BaseMaterialParamItem)
    bpy.utils.register_class(BaseMaterialChainItem)
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
    bpy.utils.unregister_class(BaseMaterialChainItem)
    bpy.utils.unregister_class(BaseMaterialParamItem)
    bpy.utils.unregister_class(BaseMaterialPathItem)
    bpy.utils.unregister_class(NodeGroupInputProperties) #! imp to reg first
    # if update_node_group_inputs in bpy.app.handlers.depsgraph_update_post:
    #     bpy.app.handlers.depsgraph_update_post.remove(update_node_group_inputs)
    #for handle in bpy.app.handlers.depsgraph_update_post:
    del bpy.types.Material.witcher_props
    del bpy.types.Node.witcher_include
    del bpy.types.Node.witcher_export
    del bpy.types.Node.witcher_final_path
    del bpy.types.Node.witcher_param_kind
    del bpy.types.Node.witcher_param_name
    del bpy.types.Node.witcher_vector_source
    del bpy.types.Node.witcher_vector_w
    del bpy.types.Node.witcher_texarray_source_path
    del bpy.types.Node.witcher_texarray_path_manual
    del bpy.types.Node.witcher_texture_source_path
    del bpy.types.Node.witcher_texture_path_manual
    #bpy.types.SpaceNodeEditor.draw_handler_remove(open_menu, 'WINDOW')
