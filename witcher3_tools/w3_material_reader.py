import logging
import os
from typing import Any, Dict, List, Optional, Set
from xml.etree.ElementTree import Element

from .CR2W import CR2W_reader
from .CR2W.bin_helpers import ReadVLQInt32, readU32, readUByte, readUShort
from .CR2W.common_blender import repo_file, win_path_key, win_safe_path

log = logging.getLogger(__name__)


def normalize_depot_path(path: str) -> str:
    return (path or "").replace("/", "\\").lower()


def _get_prop_handle_depot_path(prop) -> str:
    if prop is None:
        return ""
    for handle in getattr(prop, "Handles", []) or []:
        depot_path = getattr(handle, "DepotPath", None)
        if depot_path:
            return depot_path
    return ""


def _find_chunk_handle_path(chunk, *preferred_names: str) -> str:
    checked_names: Set[str] = set()
    for name in preferred_names:
        if not name:
            continue
        checked_names.add(name)
        try:
            prop = chunk.GetVariableByName(name)
        except Exception:
            prop = None
        depot_path = _get_prop_handle_depot_path(prop)
        if depot_path:
            return depot_path

    for prop in getattr(chunk, "PROPS", []) or []:
        prop_name = str(getattr(prop, "theName", "") or "")
        if prop_name in checked_names:
            continue
        depot_path = _get_prop_handle_depot_path(prop)
        if depot_path:
            return depot_path
    return ""


_material_param_cache: Dict[str, Dict[str, tuple[str, str]]] = {}
_resolved_w2mg_cache: Dict[str, Optional[str]] = {}
_graph_buffer_meta_cache: Dict[str, Dict[str, object]] = {}
_graph_declared_param_cache: Dict[str, Optional[Set[str]]] = {}

GRAPH_DECLARED_INSTANCE_PARAMS: Set[str] = {"SpecularColor"}

# w2mg parameters default to 1.0 if the scaler value isn't defined explicitly
GRAPH_IMPLICIT_PARAM_DEFAULTS: Dict[str, tuple[str, str]] = {
    "CMaterialParameterScalar": ("Float", "1.0"),
    "CMaterialParameterColor": ("Color", "255; 255; 255; 255"),
}


def _implicit_graph_param_default(chunk_type: str) -> Optional[tuple[str, str]]:
    return GRAPH_IMPLICIT_PARAM_DEFAULTS.get(chunk_type)


def _apply_implicit_graph_param_default(
        final_params: Dict[str, tuple[str, str]],
        par_name: str,
        chunk_type: str,
        material_path: Optional[str] = None
        ) -> None:
    implicit_default = _implicit_graph_param_default(chunk_type)
    if implicit_default is None:
        return
    final_params[par_name] = implicit_default
    log.debug(
        "Using implicit graph default for '%s' from '%s' -> %s",
        par_name,
        material_path or "<unknown>",
        implicit_default[1],
    )


def _load_material_root_chunk(material_path: str):
    full_path = repo_file(material_path)
    if not os.path.exists(full_path):
        return None

    material_file_chunks = CR2W_reader.load_material(full_path)
    for chunk in material_file_chunks:
        if chunk.Type in ("CMaterialInstance", "CMaterialGraph"):
            if chunk.Type == "CMaterialGraph":
                chunk._graph_params = [
                    c for c in material_file_chunks
                    if c.Type.startswith("CMaterialParameter")
                ]
                chunk._graph_material_path = material_path
                chunk._graph_buffer_meta = _read_material_graph_buffer_meta(chunk, full_path)
            return chunk
    return None


def _read_graph_parameter_buffer(file_handle, cr2w_file) -> List[Dict[str, object]]:
    params: List[Dict[str, object]] = []
    count = ReadVLQInt32(file_handle)
    if count <= 0:
        return params

    for _ in range(count):
        param_type = readUByte(file_handle)
        offset = readUByte(file_handle)
        name_index = readUShort(file_handle)
        try:
            name = cr2w_file.CNAMES[name_index].name.value
        except Exception:
            name = ""
        params.append({
            "type": param_type,
            "offset": offset,
            "name": name,
        })
    return params


def _read_material_graph_buffer_meta(material_bin, full_path: str) -> Dict[str, object]:
    cache_key = win_path_key(full_path)
    cached = _graph_buffer_meta_cache.get(cache_key)
    if cached is not None:
        return cached

    cr2w_file = getattr(material_bin, "_W_CLASS__CR2WFILE", None)
    if cr2w_file is None:
        return {}

    try:
        export = cr2w_file.CR2WExport[material_bin.ChunkIndex]
    except Exception:
        return {}

    prop_end = max((getattr(prop, "dataEnd", 0) for prop in getattr(material_bin, "PROPS", [])), default=export.dataOffset + 1)
    buffer_start = prop_end + 2
    export_end = export.dataOffset + export.dataSize
    if buffer_start >= export_end:
        return {}

    meta: Dict[str, object] = {}
    try:
        with open(win_safe_path(full_path), "rb") as file_handle:
            file_handle.seek(buffer_start)
            meta["pixel_params"] = _read_graph_parameter_buffer(file_handle, cr2w_file)
            meta["vertex_params"] = _read_graph_parameter_buffer(file_handle, cr2w_file)
            if file_handle.tell() + 4 <= export_end:
                meta["unk1"] = readU32(file_handle)
    except Exception as exc:
        log.warning("Failed to read buffered graph params from '%s': %s", full_path, exc)
        meta = {}

    _graph_buffer_meta_cache[cache_key] = meta
    return meta


def read_declared_graph_params(material_path: str) -> Optional[Set[str]]:
    if not material_path:
        return None

    graph_path = material_path
    if material_path.lower().endswith(".w2mi"):
        resolved_graph = resolve_w2mg(material_path)
        if not resolved_graph:
            return None
        graph_path = resolved_graph

    normalized_path = normalize_depot_path(graph_path)
    if normalized_path in _graph_declared_param_cache:
        cached = _graph_declared_param_cache[normalized_path]
        return set(cached) if cached is not None else None

    material_bin = _load_material_root_chunk(graph_path)
    if material_bin is None or material_bin.Type != "CMaterialGraph":
        _graph_declared_param_cache[normalized_path] = None
        return None

    declared_params: Set[str] = set()
    for chunk in getattr(material_bin, "_graph_params", []) or []:
        name_var = chunk.GetVariableByName('parameterName')
        if name_var is None:
            continue
        par_name = getattr(getattr(name_var, "Index", None), "String", None)
        if par_name:
            declared_params.add(par_name)

    graph_meta = getattr(material_bin, "_graph_buffer_meta", None) or {}
    for buffer_name in ("pixel_params", "vertex_params"):
        for graph_param in graph_meta.get(buffer_name, []):
            par_name = graph_param.get("name")
            if par_name:
                declared_params.add(par_name)

    cached_value = set(declared_params)
    _graph_declared_param_cache[normalized_path] = cached_value
    return set(cached_value)


def prune_unsupported_instance_params(
        xml_data: Element,
        shader_graph_path: str,
        params: Optional[Dict[str, str]] = None
        ) -> None:
    declared_params = read_declared_graph_params(shader_graph_path)
    if declared_params is None:
        return

    for param in list(xml_data):
        par_name = param.get('name')
        if par_name not in GRAPH_DECLARED_INSTANCE_PARAMS:
            continue
        if par_name in declared_params:
            continue

        xml_data.remove(param)
        if params is not None:
            params.pop(par_name, None)
        log.info(
            "Skipping stale instance param '%s': shader graph '%s' does not declare it",
            par_name,
            shader_graph_path,
        )


def _read_material_params_from_bin(material_bin, seen_paths: Optional[Set[str]] = None):
    final_params: Dict[str, tuple[str, str]] = {}
    base_material = material_bin.GetVariableByName('baseMaterial')
    if base_material and getattr(base_material, "Handles", None):
        handle = base_material.Handles[0]
        base_path = getattr(handle, "DepotPath", None)
        if base_material.theType == "handle:IMaterial" and base_path:
            final_params.update(read_material_params_from_path(base_path, seen_paths=seen_paths))
    read_instance_params(material_bin, final_params)
    return final_params


def read_material_params_from_path(
        material_path: str,
        seen_paths: Optional[Set[str]] = None
        ) -> Dict[str, tuple[str, str]]:
    if not material_path:
        return {}

    normalized_path = normalize_depot_path(material_path)
    cached = _material_param_cache.get(normalized_path)
    if cached is not None:
        return dict(cached)

    if seen_paths is None:
        seen_paths = set()
    if normalized_path in seen_paths:
        log.warning("Detected cyclic material inheritance while reading '%s'", material_path)
        return {}

    seen_paths.add(normalized_path)
    try:
        material_bin = _load_material_root_chunk(material_path)
        if material_bin is None:
            return {}

        params = _read_material_params_from_bin(material_bin, seen_paths=seen_paths)
        _material_param_cache[normalized_path] = dict(params)
        return dict(params)
    finally:
        seen_paths.discard(normalized_path)


def resolve_w2mg(w2mi_path):
    """Follow w2mi baseMaterial chain to find the final .w2mg shader path."""
    if not w2mi_path:
        return None

    normalized_path = normalize_depot_path(w2mi_path)
    if normalized_path in _resolved_w2mg_cache:
        return _resolved_w2mg_cache[normalized_path]

    try:
        material_bin = _load_material_root_chunk(w2mi_path)
        if material_bin is None:
            _resolved_w2mg_cache[normalized_path] = None
            return None
        base_var = material_bin.GetVariableByName('baseMaterial')
        if not base_var:
            _resolved_w2mg_cache[normalized_path] = None
            return None
        base_path = base_var.Handles[0].DepotPath
        if base_path.endswith(".w2mg"):
            _resolved_w2mg_cache[normalized_path] = base_path
            return base_path
        if base_path.endswith(".w2mi"):
            resolved = resolve_w2mg(base_path)
            _resolved_w2mg_cache[normalized_path] = resolved
            return resolved
    except Exception:
        pass
    _resolved_w2mg_cache[normalized_path] = None
    return None


def read_local_material_params_from_bin(material_bin) -> Dict[str, tuple[str, str]]:
    local_params: Dict[str, tuple[str, str]] = {}
    if material_bin is None:
        return local_params
    read_instance_params(material_bin, local_params)
    return local_params


def collect_material_chain(material_path: str) -> Dict[str, Any]:
    normalized_path = normalize_depot_path(material_path)
    result: Dict[str, Any] = {
        "requested_path": material_path or "",
        "normalized_path": normalized_path,
        "resolved_graph": "",
        "chain": [],
        "warnings": [],
        "errors": [],
    }
    if not material_path:
        result["errors"].append("Base Path is empty.")
        return result

    current_path = material_path
    seen_paths: Set[str] = set()
    while current_path:
        normalized_current = normalize_depot_path(current_path)
        if normalized_current in seen_paths:
            result["errors"].append(f"Cyclic material inheritance detected at '{current_path}'.")
            break
        seen_paths.add(normalized_current)

        material_bin = _load_material_root_chunk(current_path)
        if material_bin is None:
            result["errors"].append(f"Could not read material '{current_path}'.")
            break

        chunk_type = str(getattr(material_bin, "Type", "") or "")
        source_kind = "graph" if chunk_type == "CMaterialGraph" else "instance"
        result["chain"].append({
            "path": current_path,
            "normalized_path": normalized_current,
            "chunk_type": chunk_type,
            "source_kind": source_kind,
            "_material_bin": material_bin,
        })

        if chunk_type == "CMaterialGraph":
            result["resolved_graph"] = current_path
            break

        base_var = material_bin.GetVariableByName('baseMaterial')
        next_path = ""
        if base_var and getattr(base_var, "Handles", None):
            next_path = getattr(base_var.Handles[0], "DepotPath", "") or ""
        if not next_path:
            result["errors"].append(f"Material instance '{current_path}' has no readable baseMaterial.")
            break
        current_path = next_path

    if not result["resolved_graph"] and not result["errors"]:
        result["errors"].append(f"Could not resolve a .w2mg from '{material_path}'.")
    return result


def _read_graph_param_chunks(
        chunks,
        final_params,
        graph_meta: Optional[Dict[str, object]] = None,
        material_path: Optional[str] = None
        ):
    """Read CMaterialParameter* chunks from a .w2mg graph file into final_params."""
    for chunk in chunks:
        name_var = chunk.GetVariableByName('parameterName')
        if name_var is None:
            continue
        par_name = name_var.Index.String
        if not par_name:
            continue
        try:
            if chunk.Type == "CMaterialParameterTexture":
                texture_path = _find_chunk_handle_path(chunk, 'texture')
                if texture_path:
                    final_params[par_name] = ('handle:ITexture', texture_path)
            elif chunk.Type == "CMaterialParameterCube":
                cubemap_path = _find_chunk_handle_path(chunk, 'cube', 'cubemap', 'texture')
                if cubemap_path:
                    final_params[par_name] = ('handle:CCubeTexture', cubemap_path)
            elif chunk.Type == "CMaterialParameterColor":
                color = chunk.GetVariableByName('color')
                if color:
                    red = color.GetVariableByName('Red').Value
                    green = color.GetVariableByName('Green').Value
                    blue = color.GetVariableByName('Blue').Value
                    alpha = color.GetVariableByName('Alpha').Value
                    final_params[par_name] = ('Color', f"{red}; {green}; {blue}; {alpha}")
                else:
                    _apply_implicit_graph_param_default(final_params, par_name, chunk.Type, material_path)
            elif chunk.Type == "CMaterialParameterScalar":
                scalar = chunk.GetVariableByName('scalar')
                if scalar:
                    final_params[par_name] = ('Float', str(scalar.Value))
                else:
                    _apply_implicit_graph_param_default(final_params, par_name, chunk.Type, material_path)
            elif chunk.Type == "CMaterialParameterVector":
                vector = chunk.GetVariableByName('vector')
                if vector:
                    x = vector.GetVariableByName('X').Value
                    y = vector.GetVariableByName('Y').Value
                    z = vector.GetVariableByName('Z').Value
                    w = vector.GetVariableByName('W').Value
                    final_params[par_name] = ('Vector', f"{x}; {y}; {z}; {w}")
            elif chunk.Type == "CMaterialParameterTextureArray":
                texture_array_path = _find_chunk_handle_path(chunk, 'textureArray', 'texture', 'textures')
                if texture_array_path:
                    final_params[par_name] = ('handle:CTextureArray', texture_array_path)
        except Exception as exc:
            log.warning("Failed to read graph param chunk %s '%s': %s", chunk.Type, par_name, exc)


def read_instance_params(material, final_params):
    mat_instance = getattr(material, 'CMaterialInstance', None)
    if mat_instance is None:
        graph_params = getattr(material, '_graph_params', None)
        if graph_params:
            _read_graph_param_chunks(
                graph_params,
                final_params,
                graph_meta=getattr(material, '_graph_buffer_meta', None),
                material_path=getattr(material, '_graph_material_path', None),
            )
        return final_params
    for mat_param in mat_instance.InstanceParameters.elements:
        prop = mat_param.PROP
        if prop.theType == "Float":
            final_params[prop.theName] = (prop.theType, str(prop.Value))
        elif prop.theType == "Vector" or prop.theType == "Color":
            the_value = (
                str(prop.More[0].Value) + "; "
                + str(prop.More[1].Value) + "; "
                + str(prop.More[2].Value) + "; "
                + str(prop.More[3].Value)
            )
            final_params[prop.theName] = (prop.theType, the_value)
        elif prop.theType in ("handle:ITexture", "handle:CTextureArray", "handle:CCubeTexture"):
            if prop.Handles[0].DepotPath:
                file_path = prop.Handles[0].DepotPath
                final_params[prop.theName] = (prop.theType, file_path)
        else:
            log.warning('Unsupported param type in CR2W "%s"', prop.theType)
    return final_params
