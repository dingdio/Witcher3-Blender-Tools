"""Shared material-chain display helpers."""

CHAIN_NODE_COLORS = [
    (0.62, 0.42, 0.16),
    (0.18, 0.52, 0.47),
    (0.22, 0.36, 0.72),
    (0.62, 0.26, 0.46),
    (0.40, 0.56, 0.20),
    (0.50, 0.34, 0.68),
    (0.68, 0.48, 0.18),
    (0.24, 0.48, 0.66),
    (0.70, 0.30, 0.22),
    (0.28, 0.58, 0.32),
    (0.50, 0.44, 0.16),
    (0.36, 0.30, 0.76),
    (0.68, 0.34, 0.62),
    (0.24, 0.62, 0.58),
    (0.56, 0.38, 0.24),
    (0.42, 0.50, 0.70),
]

CHAIN_NODE_GROUP_X_OFFSET = 700
CHAIN_NODE_X_STEP = 260
LOCAL_NODE_GROUP_X_OFFSET = 420
LOCAL_NODE_COLOR = (0.12, 0.46, 0.74)
CHAIN_NODE_ROW_Y = 1000
CHAIN_NODE_ROW_STEP = 120

_CHAIN_NODE_ROW_STEPS = {
    "Float": 90,
    "Color": 135,
    "Vector": 115,
    "handle:ITexture": 245,
    "handle:CTextureArray": 245,
    "handle:CCubeTexture": 245,
}


def coerce_source_index(value) -> int:
    try:
        return int(value)
    except Exception:
        return -1


def chain_color_for_index(source_index: int):
    source_index = coerce_source_index(source_index)
    if source_index < 0:
        return None
    return CHAIN_NODE_COLORS[source_index % len(CHAIN_NODE_COLORS)]


def chain_node_x(node_group_x: int, source_index: int) -> int:
    source_index = max(0, coerce_source_index(source_index))
    return int(node_group_x) - CHAIN_NODE_GROUP_X_OFFSET - (source_index * CHAIN_NODE_X_STEP)


def local_node_x(node_group_x: int) -> int:
    return int(node_group_x) - LOCAL_NODE_GROUP_X_OFFSET


def chain_node_y(row_index: int) -> int:
    row_index = max(0, coerce_source_index(row_index))
    return CHAIN_NODE_ROW_Y - (row_index * CHAIN_NODE_ROW_STEP)


def chain_row_step_for_type(param_type: str) -> int:
    return int(_CHAIN_NODE_ROW_STEPS.get(str(param_type or ""), CHAIN_NODE_ROW_STEP))
