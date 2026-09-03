"""Material node graph, path, snapshot, and export domain logic."""

import json
import logging
import os
import re
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, Optional

import bpy

from ... import (
    get_all_addon_prefs,
    get_mod_directory,
    get_modded_texture_path,
    get_texture_path,
    get_uncook_path,
)
from ...CR2W.common_blender import repo_file, win_safe_path, win_unprefix_path
from ...CR2W.witcher_cache.Bundles import LoadBundleManager
from ...extension_paths import get_cache_root
from ...repo_paths import (
    normalize_source_game as _normalize_material_source_game,
    w2_source_repo_root_if_configured,
    w2_source_roots,
)
from ..base_path import (
    create_base_material_helper,
    inspect_material_base_path,
    refresh_base_material_entry_state,
)
from ..chain import (
    CHAIN_NODE_ROW_Y,
    LOCAL_NODE_COLOR,
    chain_color_for_index,
    chain_node_x,
    chain_node_y,
    chain_row_step_for_type,
    coerce_source_index,
    local_node_x,
)
from ..constants import (
    DEFAULT_W2_MATERIAL_BASE,
    DEFAULT_W3_MATERIAL_BASE,
    WITCHER2_MATERIALS,
)
from ..material import (
    ensure_node_group_for_recommendation,
    find_group_input_socket,
    get_active_witcher_group_node,
    get_recommended_node_group_for_base_path,
    init_material_nodes,
    reconcile_w3_pattern_uv_links,
)
from ..reader import normalize_depot_path
from ..vector_param import (
    get_legacy_w_value,
    get_mapping_vector_input,
    get_vector_node_values,
    is_vector_param_node,
    mark_vector_param_node,
)


log = logging.getLogger(__name__)

_BASE_PATH_CACHE_FILE = Path(get_cache_root(create=True)) / "material_base_paths.json"
_BASE_PATH_ENUM_CACHE = {}
_NUMERIC_SUFFIX_RE = re.compile(r"\.\d{3}$")
_TEXARRAY_SLICE_RE = re.compile(r"(?i)^(.+?\.texarray)\.texture_\d+\.[^\\/]+$")
_TEXTURE_LOCATION_EXTS = (".xbm", ".dds", ".tga", ".png")

possible_folders = [
    "files\\Raw\\Mod",
    "files\\Raw\\DLC",
    "files\\Mod\\Cooked",
    "files\\Mod\\Uncooked",
    "files\\DLC\\Cooked",
    "files\\DLC\\Uncooked",
]


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
            ng = ensure_node_group_for_recommendation(recommendation)
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


_WITCHER_INCLUDE_UPDATE_SUSPENDED = False


@contextmanager
def suspend_witcher_include_updates():
    """Suppress the witcher_include update callback during bulk node setup.

    Importers set witcher_include on many nodes in a row; each set would
    otherwise trigger a full material scan + override sync + chain re-layout.
    Wrap the bulk writes in this context manager and call
    refresh_witcher_include_state(material) once afterwards.
    """
    global _WITCHER_INCLUDE_UPDATE_SUSPENDED
    previous = _WITCHER_INCLUDE_UPDATE_SUSPENDED
    _WITCHER_INCLUDE_UPDATE_SUSPENDED = True
    try:
        yield
    finally:
        _WITCHER_INCLUDE_UPDATE_SUSPENDED = previous


_WITCHER_INCLUDE_LAYOUT_SUSPENDED = False


@contextmanager
def suspend_witcher_include_layout():
    """Defer cosmetic node layout until the material's next refresh."""
    global _WITCHER_INCLUDE_LAYOUT_SUSPENDED
    previous = _WITCHER_INCLUDE_LAYOUT_SUSPENDED
    _WITCHER_INCLUDE_LAYOUT_SUSPENDED = True
    try:
        yield
    finally:
        _WITCHER_INCLUDE_LAYOUT_SUSPENDED = previous


def refresh_witcher_include_state(material):
    """Run the witcher_include sync/layout pass for a known material."""
    if material is None:
        return
    _sync_local_override_nodes(material)
    reconcile_w3_pattern_uv_links(material, get_active_witcher_group_node(material))
    if _WITCHER_INCLUDE_LAYOUT_SUSPENDED:
        material["witcher_layout_pending"] = True
        return
    if material.get("witcher_layout_pending") is not None:
        try:
            del material["witcher_layout_pending"]
        except Exception:
            pass
    _apply_chain_item_colors_to_nodes(material)
    if bool(getattr(getattr(material, "witcher_props", None), "base_read_chain_frames_enabled", True)):
        _layout_chain_nodes_by_source(material)
        _apply_chain_frames(material, create_missing=True)
    else:
        _remove_chain_frames(material)


def _update_node_witcher_include(self, context):
    if _WITCHER_INCLUDE_UPDATE_SUSPENDED:
        return
    material = _material_for_node(context, self)
    if material is None:
        return
    refresh_witcher_include_state(material)


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
    # During bulk import the caller runs refresh_witcher_include_state once at
    # the end, which repeats this exact layout/colors/frames pass — skip the
    # redundant one here.
    if not _WITCHER_INCLUDE_UPDATE_SUSPENDED:
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


_REPO_ROOT_PREP_CACHE: Dict[str, str] = {}


def _prepare_repo_root(root) -> str:
    """Resolve a candidate repo root to a validated real path ('' if unusable)."""
    root = str(root or "").strip()
    if not root:
        return ""
    cache_key = win_unprefix_path(bpy.path.abspath(root)).rstrip("\\/")
    if not cache_key:
        return ""
    cached = _REPO_ROOT_PREP_CACHE.get(cache_key)
    if cached is not None:
        return cached
    resolved = win_unprefix_path(os.path.realpath(cache_key)).rstrip("\\/")
    if not resolved or not os.path.isdir(win_safe_path(resolved)):
        resolved = ""
    _REPO_ROOT_PREP_CACHE[cache_key] = resolved
    return resolved


def _try_strip_root(path, root):
    """Strip a root directory from the path, returning game-relative path or None."""
    root = _prepare_repo_root(root)
    if not root:
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
    # A converted texture's filepath points into the _converted_textures cache; the importer records the uncook
    # source on the image, and that is the path the export must resolve.
    original = ""
    try:
        original = str(image.get("witcher_original_texture_path", "") or "")
    except Exception:
        original = ""
    rel_path = win_unprefix_path(original or getattr(image, "filepath", "") or "")
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
