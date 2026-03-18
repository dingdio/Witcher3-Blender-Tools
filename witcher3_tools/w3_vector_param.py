from typing import Optional

VECTOR_PARAM_KIND = "vector4"

VECTOR_SOURCE_XYZ = "XYZ"
VECTOR_SOURCE_LOCATION = "LOCATION"
VECTOR_SOURCE_ROTATION = "ROTATION"
VECTOR_SOURCE_SCALE = "SCALE"

_VECTOR_SOURCE_TO_MAPPING_INPUT = {
    VECTOR_SOURCE_LOCATION: "Location",
    VECTOR_SOURCE_ROTATION: "Rotation",
    VECTOR_SOURCE_SCALE: "Scale",
}


def _get_node_prop(node, name: str, default=None):
    if node is None:
        return default
    try:
        value = getattr(node, name)
        if isinstance(default, str):
            if value != "":
                return value
        elif value is not None:
            return value
    except Exception:
        pass
    try:
        return node.get(name, default)
    except Exception:
        return default


def _set_node_prop(node, name: str, value) -> None:
    if node is None:
        return
    try:
        setattr(node, name, value)
        return
    except Exception:
        pass
    try:
        node[name] = value
    except Exception:
        pass


def infer_vector_source_from_name(param_name: str) -> str:
    name = str(param_name or "").lower()
    if "rotation" in name:
        return VECTOR_SOURCE_ROTATION
    if "offset" in name or "translation" in name or "location" in name:
        return VECTOR_SOURCE_LOCATION
    if "tile" in name or "uvscale" in name or "scale" in name:
        return VECTOR_SOURCE_SCALE
    return VECTOR_SOURCE_XYZ


def get_vector_source(node, param_name: str = "") -> str:
    source = str(_get_node_prop(node, "witcher_vector_source", "") or "").upper()
    if source in {
        VECTOR_SOURCE_XYZ,
        VECTOR_SOURCE_LOCATION,
        VECTOR_SOURCE_ROTATION,
        VECTOR_SOURCE_SCALE,
    }:
        return source
    return infer_vector_source_from_name(param_name)


def set_vector_w(node, value: float) -> None:
    _set_node_prop(node, "witcher_vector_w", float(value))


def get_vector_w(node, default: float = 1.0) -> float:
    value = _get_node_prop(node, "witcher_vector_w", None)
    if value is None:
        return default if default is None else float(default)
    try:
        return float(value)
    except Exception:
        return default if default is None else float(default)


def mark_vector_param_node(node, param_name: str, w: float = 1.0, vector_source: str = ""):
    _set_node_prop(node, "witcher_param_kind", VECTOR_PARAM_KIND)
    _set_node_prop(node, "witcher_param_name", str(param_name or ""))
    _set_node_prop(node, "witcher_vector_source", get_vector_source(node, vector_source or param_name))
    set_vector_w(node, w)
    return node


def is_vector_param_node(node) -> bool:
    if node is None:
        return False
    if str(_get_node_prop(node, "witcher_param_kind", "") or "") == VECTOR_PARAM_KIND:
        return True
    return getattr(node, "type", "") in {"COMBXYZ", "MAPPING"}


def get_mapping_vector_input(node, param_name: str = ""):
    if node is None or getattr(node, "type", "") != "MAPPING":
        return None
    source = get_vector_source(node, param_name)
    socket_name = _VECTOR_SOURCE_TO_MAPPING_INPUT.get(source)
    if socket_name:
        try:
            socket = node.inputs.get(socket_name)
            if socket is not None:
                return socket
        except Exception:
            pass
    fallback_indices = {
        VECTOR_SOURCE_LOCATION: 1,
        VECTOR_SOURCE_ROTATION: 2,
        VECTOR_SOURCE_SCALE: 3,
    }
    try:
        return node.inputs[fallback_indices.get(source, 3)]
    except Exception:
        return None


def get_vector_node_values(node, param_name: str = "", default_w: float = 1.0):
    if node is None:
        return [0.0, 0.0, 0.0, float(default_w)]

    node_type = getattr(node, "type", "")
    if node_type == "COMBXYZ":
        xyz = [
            float(node.inputs[0].default_value),
            float(node.inputs[1].default_value),
            float(node.inputs[2].default_value),
        ]
    elif node_type == "MAPPING":
        vector_input = get_mapping_vector_input(node, param_name)
        if vector_input is None:
            xyz = [0.0, 0.0, 0.0]
        else:
            xyz = [float(vector_input.default_value[i]) for i in range(3)]
    else:
        xyz = [0.0, 0.0, 0.0]

    xyz.append(get_vector_w(node, default_w))
    return xyz


def get_legacy_w_value(input_socket, default: Optional[float] = 1.0):
    if input_socket is None:
        return default

    node = getattr(input_socket, "node", None)
    if node is None:
        return default

    legacy_socket = None
    try:
        legacy_socket = node.inputs.get(f"{input_socket.name}_W")
    except Exception:
        pass
    if legacy_socket is None:
        try:
            for socket in node.inputs:
                if getattr(socket, "name", None) == f"{input_socket.name}_W":
                    legacy_socket = socket
                    break
        except Exception:
            pass
    if legacy_socket is None:
        return default

    try:
        if legacy_socket.is_linked and legacy_socket.links:
            from_socket = legacy_socket.links[0].from_socket
            from_node = getattr(from_socket, "node", None)
            if from_node is not None and getattr(from_node, "type", "") == "VALUE":
                return float(from_node.outputs[0].default_value)
            return float(from_socket.default_value)
        return float(legacy_socket.default_value)
    except Exception:
        return default


def migrate_legacy_vector_socket(input_socket) -> bool:
    if input_socket is None or not getattr(input_socket, "is_linked", False):
        return False
    try:
        linked_node = input_socket.links[0].from_socket.node
    except Exception:
        return False
    if linked_node is None or getattr(linked_node, "type", "") not in {"COMBXYZ", "MAPPING"}:
        return False
    if str(_get_node_prop(linked_node, "witcher_param_kind", "") or "") == VECTOR_PARAM_KIND:
        return False

    legacy_w = get_legacy_w_value(input_socket, None)
    if legacy_w is None:
        return False
    mark_vector_param_node(linked_node, input_socket.name, legacy_w, input_socket.name)
    return True
