"""Dyng resource parsing and Blender-side simulation."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import math
import os
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

log = logging.getLogger(__name__)

Vector3 = Tuple[float, float, float]
MatrixRows = Tuple[
    Tuple[float, float, float, float],
    Tuple[float, float, float, float],
    Tuple[float, float, float, float],
    Tuple[float, float, float, float],
]

IDENTITY_MATRIX: MatrixRows = (
    (1.0, 0.0, 0.0, 0.0),
    (0.0, 1.0, 0.0, 0.0),
    (0.0, 0.0, 1.0, 0.0),
    (0.0, 0.0, 0.0, 1.0),
)

DYNG_DATA_PROP = "witcher_dyng_data"
DYNG_PARSE_STATUS_PROP = "witcher_dyng_status"
DYNG_NODE_COUNT_PROP = "witcher_dyng_node_count"
DYNG_LINK_COUNT_PROP = "witcher_dyng_link_count"
DYNG_TRIANGLE_COUNT_PROP = "witcher_dyng_triangle_count"
DYNG_COLLISION_COUNT_PROP = "witcher_dyng_collision_count"


class DyngParseError(ValueError):
    """Raised when a CDyngResource chunk is present but internally inconsistent."""


@dataclass(frozen=True)
class DyngNode:
    name: str
    parent: str
    mass: float
    stiffness: float
    distance: float
    transform: MatrixRows

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "parent": self.parent,
            "mass": self.mass,
            "stiffness": self.stiffness,
            "distance": self.distance,
            "transform": self.transform,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DyngNode":
        return cls(
            name=str(data.get("name", "")),
            parent=str(data.get("parent", "")),
            mass=float(data.get("mass", 0.0)),
            stiffness=float(data.get("stiffness", 0.0)),
            distance=float(data.get("distance", 0.0)),
            transform=_coerce_matrix(data.get("transform", IDENTITY_MATRIX)),
        )


@dataclass(frozen=True)
class DyngLink:
    type: int
    length: float
    a: int
    b: int

    def to_dict(self) -> Dict[str, Any]:
        return {"type": self.type, "length": self.length, "a": self.a, "b": self.b}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DyngLink":
        return cls(
            type=int(data.get("type", 0)),
            length=float(data.get("length", 0.0)),
            a=int(data.get("a", 0)),
            b=int(data.get("b", 0)),
        )


@dataclass(frozen=True)
class DyngTriangle:
    a: int
    b: int
    c: int

    def to_dict(self) -> Dict[str, Any]:
        return {"a": self.a, "b": self.b, "c": self.c}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DyngTriangle":
        return cls(a=int(data.get("a", 0)), b=int(data.get("b", 0)), c=int(data.get("c", 0)))


@dataclass(frozen=True)
class DyngCollision:
    parent: str
    radius: float
    height: float
    transform: MatrixRows

    def to_dict(self) -> Dict[str, Any]:
        return {
            "parent": self.parent,
            "radius": self.radius,
            "height": self.height,
            "transform": self.transform,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DyngCollision":
        return cls(
            parent=str(data.get("parent", "")),
            radius=float(data.get("radius", 0.0)),
            height=float(data.get("height", 0.0)),
            transform=_coerce_matrix(data.get("transform", IDENTITY_MATRIX)),
        )


@dataclass(frozen=True)
class DyngResourceData:
    source_path: str
    name: str
    nodes: Tuple[DyngNode, ...]
    links: Tuple[DyngLink, ...]
    triangles: Tuple[DyngTriangle, ...]
    collisions: Tuple[DyngCollision, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_path": self.source_path,
            "name": self.name,
            "nodes": [node.to_dict() for node in self.nodes],
            "links": [link.to_dict() for link in self.links],
            "triangles": [triangle.to_dict() for triangle in self.triangles],
            "collisions": [collision.to_dict() for collision in self.collisions],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DyngResourceData":
        return cls(
            source_path=str(data.get("source_path", "")),
            name=str(data.get("name", "")),
            nodes=tuple(DyngNode.from_dict(item) for item in data.get("nodes", []) or []),
            links=tuple(DyngLink.from_dict(item) for item in data.get("links", []) or []),
            triangles=tuple(DyngTriangle.from_dict(item) for item in data.get("triangles", []) or []),
            collisions=tuple(DyngCollision.from_dict(item) for item in data.get("collisions", []) or []),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"))

    @classmethod
    def from_json(cls, payload: str) -> "DyngResourceData":
        return cls.from_dict(json.loads(payload))


def _coerce_matrix(value: Any) -> MatrixRows:
    rows = list(value or IDENTITY_MATRIX)
    if len(rows) != 4:
        raise DyngParseError(f"Expected a 4-row matrix, got {len(rows)} rows")
    out = []
    for row in rows:
        row_values = list(row)
        if len(row_values) != 4:
            raise DyngParseError("Expected a 4-column matrix row")
        out.append(tuple(float(v) for v in row_values))
    return tuple(out)  # type: ignore[return-value]


def _get_prop(chunk: Any, *names: str) -> Any:
    for name in names:
        if hasattr(chunk, "GetVariableByName"):
            prop = chunk.GetVariableByName(name)
            if prop is not None:
                return prop
        for prop in getattr(chunk, "PROPS", []) or []:
            if getattr(prop, "theName", None) == name:
                return prop
    return None


def _string_value(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "ToString"):
        try:
            return str(value.ToString())
        except Exception:
            pass
    if hasattr(value, "String"):
        return str(getattr(value, "String", ""))
    return str(value)


def _read_string_array(prop: Any, label: str) -> List[str]:
    values = getattr(prop, "elements", None)
    if values is None:
        values = getattr(prop, "value", None)
    if values is None:
        values = []
    return [_string_value(item) for item in values]


def _read_number_array(prop: Any, label: str, cast: Any) -> List[Any]:
    values = getattr(prop, "value", None)
    if values is None:
        values = getattr(prop, "More", None)
    if values is None and hasattr(prop, "Value"):
        values = [getattr(prop, "Value")]
    if values is None:
        values = []
    try:
        return [cast(item) for item in values]
    except Exception as exc:
        raise DyngParseError(f"Invalid numeric array for {label}") from exc


def _read_vector_prop(prop: Any, label: str) -> Tuple[float, float, float, float]:
    values: Dict[str, float] = {}
    for child in getattr(prop, "More", []) or []:
        name = str(getattr(child, "theName", ""))
        if not name:
            continue
        if hasattr(child, "Value"):
            values[name] = float(getattr(child, "Value"))
    if not values:
        direct = []
        for attr in ("X", "Y", "Z", "W"):
            if hasattr(prop, attr):
                direct.append(float(getattr(prop, attr)))
        if len(direct) == 4:
            return tuple(direct)  # type: ignore[return-value]
    try:
        return (values["X"], values["Y"], values["Z"], values.get("W", 0.0))
    except KeyError as exc:
        raise DyngParseError(f"Invalid Vector row in {label}") from exc


def _read_matrix_element(element: Any, label: str) -> MatrixRows:
    if all(hasattr(element, attr) for attr in ("ax", "ay", "az", "aw")):
        return _coerce_matrix(
            (
                (element.ax, element.ay, element.az, element.aw),
                (element.bx, element.by, element.bz, element.bw),
                (element.cx, element.cy, element.cz, element.cw),
                (element.dx, element.dy, element.dz, element.dw),
            )
        )
    rows: Dict[str, Tuple[float, float, float, float]] = {}
    for prop in getattr(element, "MoreProps", []) or getattr(element, "More", []) or []:
        row_name = str(getattr(prop, "theName", ""))
        if row_name in {"X", "Y", "Z", "W"}:
            rows[row_name] = _read_vector_prop(prop, label)
    if set(rows) != {"X", "Y", "Z", "W"}:
        raise DyngParseError(f"Invalid Matrix element in {label}")
    return (rows["X"], rows["Y"], rows["Z"], rows["W"])


def _read_matrix_array(prop: Any, label: str) -> List[MatrixRows]:
    elements = getattr(prop, "More", None)
    if elements is None:
        elements = getattr(prop, "value", None)
    if elements is None:
        elements = []
    return [_read_matrix_element(element, label) for element in elements]


def _require_prop(chunk: Any, canonical_name: str, *aliases: str) -> Any:
    prop = _get_prop(chunk, canonical_name, f"m_{canonical_name}", *aliases)
    if prop is None:
        raise DyngParseError(f"CDyngResource is missing {canonical_name}")
    return prop


def _optional_prop(chunk: Any, canonical_name: str, *aliases: str) -> Any:
    return _get_prop(chunk, canonical_name, f"m_{canonical_name}", *aliases)


def _require_lengths(label: str, expected: int, values: Sequence[Any]) -> None:
    if len(values) != expected:
        raise DyngParseError(f"{label} has {len(values)} entries; expected {expected}")


def parse_dyng_chunk(chunk: Any, source_path: str = "") -> DyngResourceData:
    """Extract the actual CDyngResource physics arrays from a decoded CR2W chunk."""

    chunk_type = str(getattr(chunk, "name", getattr(chunk, "Type", "")) or "")
    if chunk_type and chunk_type != "CDyngResource":
        # Some decoded objects expose Type instead of name; accept either, but
        # keep this guard to catch accidental CSkeleton parsing.
        if str(getattr(chunk, "Type", "") or "") != "CDyngResource":
            raise DyngParseError(f"Expected CDyngResource, got {chunk_type}")

    name_prop = _get_prop(chunk, "name", "m_name")
    resource_name = ""
    if name_prop is not None:
        if hasattr(name_prop, "String"):
            resource_name = _string_value(getattr(name_prop, "String"))
        elif hasattr(name_prop, "Index"):
            resource_name = _string_value(getattr(name_prop, "Index"))

    node_names = _read_string_array(_require_prop(chunk, "nodeNames"), "nodeNames")
    node_parents = _read_string_array(_require_prop(chunk, "nodeParents"), "nodeParents")
    node_masses = _read_number_array(_require_prop(chunk, "nodeMasses"), "nodeMasses", float)
    node_stiffnesses = _read_number_array(
        _require_prop(chunk, "nodeStifnesses", "nodeStiffnesses"),
        "nodeStifnesses",
        float,
    )
    node_distances = _read_number_array(_require_prop(chunk, "nodeDistances"), "nodeDistances", float)
    node_transforms = _read_matrix_array(_require_prop(chunk, "nodeTransforms"), "nodeTransforms")

    node_count = len(node_names)
    for label, values in (
        ("nodeParents", node_parents),
        ("nodeMasses", node_masses),
        ("nodeStifnesses", node_stiffnesses),
        ("nodeDistances", node_distances),
        ("nodeTransforms", node_transforms),
    ):
        _require_lengths(label, node_count, values)

    link_types_prop = _optional_prop(chunk, "linkTypes")
    if link_types_prop is None:
        link_types = []
        link_lengths = []
        link_as = []
        link_bs = []
    else:
        link_types = _read_number_array(link_types_prop, "linkTypes", int)
        link_lengths = _read_number_array(_require_prop(chunk, "linkLengths"), "linkLengths", float)
        link_as = _read_number_array(_require_prop(chunk, "linkAs", "linksA"), "linkAs", int)
        link_bs = _read_number_array(_require_prop(chunk, "linkBs", "linksB"), "linkBs", int)
    link_count = len(link_types)
    for label, values in (("linkLengths", link_lengths), ("linkAs", link_as), ("linkBs", link_bs)):
        _require_lengths(label, link_count, values)

    triangle_as_prop = _optional_prop(chunk, "triangleAs", "trianglesA")
    if triangle_as_prop is None:
        triangle_as = []
        triangle_bs = []
        triangle_cs = []
    else:
        triangle_as = _read_number_array(triangle_as_prop, "triangleAs", int)
        triangle_bs = _read_number_array(_require_prop(chunk, "triangleBs", "trianglesB"), "triangleBs", int)
        triangle_cs = _read_number_array(_require_prop(chunk, "triangleCs", "trianglesC"), "triangleCs", int)
    triangle_count = len(triangle_as)
    for label, values in (("triangleBs", triangle_bs), ("triangleCs", triangle_cs)):
        _require_lengths(label, triangle_count, values)

    collision_parents_prop = _optional_prop(chunk, "collisionParents")
    if collision_parents_prop is None:
        collision_parents = []
        collision_radii = []
        collision_heights = []
        collision_transforms = []
    else:
        collision_parents = _read_string_array(collision_parents_prop, "collisionParents")
        collision_radii = _read_number_array(_require_prop(chunk, "collisionRadiuses"), "collisionRadiuses", float)
        collision_heights = _read_number_array(_require_prop(chunk, "collisionHeights"), "collisionHeights", float)
        collision_transforms = _read_matrix_array(_require_prop(chunk, "collisionTransforms"), "collisionTransforms")
    collision_count = len(collision_parents)
    for label, values in (
        ("collisionRadiuses", collision_radii),
        ("collisionHeights", collision_heights),
        ("collisionTransforms", collision_transforms),
    ):
        _require_lengths(label, collision_count, values)

    for link in zip(link_as, link_bs):
        for idx in link:
            if idx < 0 or idx >= node_count:
                raise DyngParseError(f"Link references invalid node index {idx}")

    nodes = tuple(
        DyngNode(node_names[i], node_parents[i], node_masses[i], node_stiffnesses[i], node_distances[i], node_transforms[i])
        for i in range(node_count)
    )
    links = tuple(DyngLink(link_types[i], link_lengths[i], link_as[i], link_bs[i]) for i in range(link_count))
    triangles = tuple(DyngTriangle(triangle_as[i], triangle_bs[i], triangle_cs[i]) for i in range(triangle_count))
    collisions = tuple(
        DyngCollision(collision_parents[i], collision_radii[i], collision_heights[i], collision_transforms[i])
        for i in range(collision_count)
    )
    return DyngResourceData(
        source_path=os.fspath(source_path) if source_path else "",
        name=resource_name,
        nodes=nodes,
        links=links,
        triangles=triangles,
        collisions=collisions,
    )


def load_dyng_resource(filename: str) -> DyngResourceData:
    """Load CDyngResource arrays from a cooked ``.w3dyng`` file."""

    from ..CR2W.CR2W_types import getCR2W, open_cr2w_read_stream

    stream = open_cr2w_read_stream(filename)
    cr2w_file = getCR2W(stream)
    chunks = getattr(getattr(cr2w_file, "CHUNKS", None), "CHUNKS", None) or []
    for chunk in chunks:
        if str(getattr(chunk, "name", getattr(chunk, "Type", "")) or "") == "CDyngResource":
            return parse_dyng_chunk(chunk, source_path=filename)
    raise DyngParseError(f"No CDyngResource chunk found in {filename}")


def store_dyng_resource_on_object(obj: Any, resource: DyngResourceData) -> None:
    obj[DYNG_DATA_PROP] = resource.to_json()
    obj[DYNG_NODE_COUNT_PROP] = len(resource.nodes)
    obj[DYNG_LINK_COUNT_PROP] = len(resource.links)
    obj[DYNG_TRIANGLE_COUNT_PROP] = len(resource.triangles)
    obj[DYNG_COLLISION_COUNT_PROP] = len(resource.collisions)
    obj[DYNG_PARSE_STATUS_PROP] = "Dyng data loaded"


def attach_dyng_resource_to_object(obj: Any, filename: str) -> Optional[DyngResourceData]:
    try:
        resource = load_dyng_resource(filename)
    except Exception as exc:
        try:
            obj[DYNG_PARSE_STATUS_PROP] = f"Dyng parse failed: {exc}"
        except Exception:
            pass
        log.warning("Failed to parse Dyng resource %s", filename, exc_info=True)
        return None
    store_dyng_resource_on_object(obj, resource)
    return resource


def resource_from_object(obj: Any) -> Optional[DyngResourceData]:
    payload = obj.get(DYNG_DATA_PROP) if hasattr(obj, "get") else None
    if not payload:
        return None
    try:
        return DyngResourceData.from_json(str(payload))
    except Exception:
        log.warning("Invalid stored Dyng data on %s", getattr(obj, "name", obj), exc_info=True)
        return None


def _v_add(a: Vector3, b: Vector3) -> Vector3:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _v_sub(a: Vector3, b: Vector3) -> Vector3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _v_mul(a: Vector3, value: float) -> Vector3:
    return (a[0] * value, a[1] * value, a[2] * value)


def _v_div(a: Vector3, value: float) -> Vector3:
    if abs(value) <= 1e-12:
        return (0.0, 0.0, 0.0)
    return (a[0] / value, a[1] / value, a[2] / value)


def _v_dot(a: Vector3, b: Vector3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _v_cross(a: Vector3, b: Vector3) -> Vector3:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _v_len(a: Vector3) -> float:
    return math.sqrt(_v_dot(a, a))


def _v_norm(a: Vector3) -> Tuple[Vector3, float]:
    length = _v_len(a)
    if length <= 1e-12:
        return (0.0, 0.0, 0.0), 0.0
    return _v_div(a, length), length


def _v_clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def matrix_translation(matrix: Sequence[Sequence[float]]) -> Vector3:
    return (float(matrix[3][0]), float(matrix[3][1]), float(matrix[3][2]))


def matrix_with_translation(matrix: Sequence[Sequence[float]], position: Vector3) -> List[List[float]]:
    out = [list(row) for row in matrix]
    out[3][0], out[3][1], out[3][2], out[3][3] = position[0], position[1], position[2], 1.0
    return out


def transform_from_axes(x_axis: Vector3, y_axis: Vector3, z_axis: Vector3, position: Vector3) -> MatrixRows:
    return (
        (float(x_axis[0]), float(x_axis[1]), float(x_axis[2]), 0.0),
        (float(y_axis[0]), float(y_axis[1]), float(y_axis[2]), 0.0),
        (float(z_axis[0]), float(z_axis[1]), float(z_axis[2]), 0.0),
        (float(position[0]), float(position[1]), float(position[2]), 1.0),
    )


def matrix_mul(a: Sequence[Sequence[float]], b: Sequence[Sequence[float]]) -> List[List[float]]:
    return [
        [sum(float(a[row][k]) * float(b[k][col]) for k in range(4)) for col in range(4)]
        for row in range(4)
    ]


def transform_point(matrix: Sequence[Sequence[float]], point: Vector3) -> Vector3:
    x, y, z = point
    return (
        x * matrix[0][0] + y * matrix[1][0] + z * matrix[2][0] + matrix[3][0],
        x * matrix[0][1] + y * matrix[1][1] + z * matrix[2][1] + matrix[3][1],
        x * matrix[0][2] + y * matrix[1][2] + z * matrix[2][2] + matrix[3][2],
    )


def _copy_transforms(transforms: Sequence[Sequence[Sequence[float]]]) -> List[List[List[float]]]:
    return [[list(row) for row in transform] for transform in transforms]


def _orient_x_axis_to(matrix: Sequence[Sequence[float]], direction: Vector3, position: Vector3) -> List[List[float]]:
    to_lookat, length = _v_norm(direction)
    if length <= 1e-8:
        return matrix_with_translation(matrix, position)

    old_x = (float(matrix[0][0]), float(matrix[0][1]), float(matrix[0][2]))
    old_x_unit, old_x_len = _v_norm(old_x)
    if old_x_len <= 1e-8:
        return matrix_with_translation(matrix, position)

    axis, axis_len = _v_norm(_v_cross(old_x_unit, to_lookat))
    if axis_len <= 1e-4:
        return matrix_with_translation(matrix, position)

    angle = math.acos(_v_clamp(_v_dot(old_x_unit, to_lookat), -1.0, 1.0))
    sin_angle = math.sin(angle)
    cos_angle = math.cos(angle)

    def rotate_row(row: Sequence[float]) -> List[float]:
        vec = (float(row[0]), float(row[1]), float(row[2]))
        cross = _v_cross(axis, vec)
        dot = _v_dot(axis, vec)
        rotated = _v_add(
            _v_add(_v_mul(vec, cos_angle), _v_mul(cross, sin_angle)),
            _v_mul(axis, dot * (1.0 - cos_angle)),
        )
        return [rotated[0], rotated[1], rotated[2], 0.0]

    out = [rotate_row(matrix[row]) for row in range(3)]
    out.append([float(position[0]), float(position[1]), float(position[2]), 1.0])
    return out


def _unit_noise(step: int, index: int, salt: int) -> float:
    value = math.sin((step + 1) * 12.9898 + (index + 1) * 78.233 + (salt + 1) * 37.719) * 43758.5453
    return value - math.floor(value)


class DyngSimulator:
    """Position-based Dyng solver for Blender preview."""

    _GRAVITY_ACCEL = 19.62
    _WIND_ACCEL = 18.0

    def __init__(self, resource: DyngResourceData, target_transforms: Sequence[Sequence[Sequence[float]]]):
        if len(resource.nodes) != len(target_transforms):
            raise ValueError("Dyng target transform count must match node count")
        self.resource = resource
        self.use_offsets = False
        self.plane_collision = False
        self.body_collision = False
        self.body_collision_radius = 0.0
        self.body_collision_strength = 1.0
        self.dampening = 0.95
        self.dt = 0.016666667
        self.shaking = 0.0
        self.max_link_iterations = 10
        self.gravity = 1.0
        self.relaxed_mode = False
        self._step_index = 0

        node_count = len(resource.nodes)
        self.positions: List[Vector3] = [matrix_translation(t) for t in target_transforms]
        self.velocities: List[Vector3] = [(0.0, 0.0, 0.0) for _ in range(node_count)]
        self.lookats: List[int] = [-1 for _ in range(node_count)]
        self.masses: List[float] = [float(node.mass) for node in resource.nodes]
        self.global_transforms: List[List[List[float]]] = _copy_transforms(target_transforms)
        self.offsets: List[List[List[float]]] = _copy_transforms([IDENTITY_MATRIX for _ in range(node_count)])
        self.stiffnesses: List[float] = [float(node.stiffness) for node in resource.nodes]
        self.shake_weights: List[float] = [0.0 for _ in range(node_count)]
        self.distances: List[float] = [float(node.distance) for node in resource.nodes]
        self.orientation_radii: List[float] = [0.3 for _ in range(node_count)]
        self.body_radii: List[float] = [0.0 for _ in range(node_count)]

        self.link_a: List[int] = [link.a for link in resource.links]
        self.link_b: List[int] = [link.b for link in resource.links]
        self.link_lengths: List[float] = [float(link.length) for link in resource.links]
        self.link_types: List[int] = [int(link.type) for link in resource.links]
        self._link_shares: List[Tuple[float, float]] = []

        self._read_collision_helpers()
        self._choose_orientation_targets()
        self._prepare_link_shares()

    def _read_collision_helpers(self) -> None:
        for index, collision in enumerate(self.resource.collisions[: len(self.resource.nodes)]):
            self.offsets[index] = [list(row) for row in collision.transform]
            self.orientation_radii[index] = float(collision.height)
            self.body_radii[index] = max(
                0.0,
                abs(float(collision.radius)),
                abs(float(collision.height)),
            )
            self.shake_weights[index] = float(collision.radius)

    def _choose_orientation_targets(self) -> None:
        node_count = len(self.resource.nodes)
        for triangle in self.resource.triangles:
            if 0 <= triangle.a < node_count and 0 <= triangle.b < node_count:
                self.lookats[triangle.a] = triangle.b

        name_to_index = {node.name: index for index, node in enumerate(self.resource.nodes)}
        child_count = [0 for _ in self.resource.nodes]
        for index, node in enumerate(self.resource.nodes):
            parent_index = name_to_index.get(node.parent, -1)
            if parent_index < 0:
                continue
            if child_count[parent_index] == 0 and self.orientation_radii[index] > 0.0 and self.lookats[index] == -1:
                self.lookats[parent_index] = index
            elif child_count[parent_index] > 0:
                self.lookats[parent_index] = -1
            child_count[parent_index] += 1

    def _mobility(self, index: int) -> float:
        if index < 0 or index >= len(self.distances) or self.distances[index] <= 0.0:
            return 0.0
        return max(abs(self.masses[index]), 0.001)

    def _prepare_link_shares(self) -> None:
        self._link_shares = []
        for node_a, node_b in zip(self.link_a, self.link_b):
            mobility_a = self._mobility(node_a)
            mobility_b = self._mobility(node_b)
            total = mobility_a + mobility_b
            if total <= 1e-12:
                share_a, share_b = 0.0, 0.0
            else:
                share_a = mobility_a / total
                share_b = mobility_b / total
            self._link_shares.append((share_a, share_b))

    def force_reset(self, target_transforms: Optional[Sequence[Sequence[Sequence[float]]]] = None) -> None:
        if target_transforms is not None:
            self.global_transforms = _copy_transforms(target_transforms)
        self._step_index = 0
        for index, transform in enumerate(self.global_transforms):
            self.velocities[index] = (0.0, 0.0, 0.0)
            self.positions[index] = self._anchor_position(index, transform)

    def step(
        self,
        target_transforms: Sequence[Sequence[Sequence[float]]],
        dt: float,
        *,
        speed: float = 1.0,
        dampening: Optional[float] = None,
        gravity: Optional[float] = None,
        wind: float = 0.0,
        wind_vector: Vector3 = (0.0, 0.0, 0.0),
        use_offsets: Optional[bool] = None,
        plane_collision: Optional[bool] = None,
        body_collision: Optional[bool] = None,
        body_collision_radius: Optional[float] = None,
        body_collision_strength: Optional[float] = None,
        shake: Optional[float] = None,
        max_link_iterations: Optional[int] = None,
        force_reset: bool = False,
        relaxed: bool = False,
    ) -> List[List[List[float]]]:
        if len(target_transforms) != len(self.resource.nodes):
            raise ValueError("Dyng target transform count must match node count")
        self._apply_step_settings(
            dampening=dampening,
            gravity=gravity,
            use_offsets=use_offsets,
            plane_collision=plane_collision,
            body_collision=body_collision,
            body_collision_radius=body_collision_radius,
            body_collision_strength=body_collision_strength,
            shake=shake,
            max_link_iterations=max_link_iterations,
        )

        self.global_transforms = _copy_transforms(target_transforms)
        effective_dt = min(max(float(dt), 0.0), 0.02) * max(float(speed), 0.0)
        if force_reset:
            self.force_reset(target_transforms)
        if effective_dt <= 0.00001:
            self._pin_anchors()
            self._rebuild_transforms()
            return self.global_transforms

        self.dt = effective_dt
        self._step_index += 1
        self.relaxed_mode = bool(relaxed)
        substeps = 30 if relaxed else 1
        sub_dt = effective_dt / substeps
        for _ in range(substeps):
            previous = list(self.positions)
            self._pin_anchors()
            self._integrate_free_nodes(sub_dt, wind, wind_vector)
            self._project_anchor_limits()
            for _iteration in range(max(0, self.max_link_iterations)):
                self._project_links()
            self._project_body_colliders()
            self._project_anchor_limits()
            self._update_velocities(previous, sub_dt)
        self.relaxed_mode = False
        self._rebuild_transforms()
        return self.global_transforms

    def _apply_step_settings(
        self,
        *,
        dampening: Optional[float],
        gravity: Optional[float],
        use_offsets: Optional[bool],
        plane_collision: Optional[bool],
        body_collision: Optional[bool],
        body_collision_radius: Optional[float],
        body_collision_strength: Optional[float],
        shake: Optional[float],
        max_link_iterations: Optional[int],
    ) -> None:
        if dampening is not None:
            self.dampening = float(dampening)
        if gravity is not None:
            self.gravity = float(gravity)
        if use_offsets is not None:
            self.use_offsets = bool(use_offsets)
        if plane_collision is not None:
            self.plane_collision = bool(plane_collision)
        if body_collision is not None:
            self.body_collision = bool(body_collision)
        if body_collision_radius is not None:
            self.body_collision_radius = max(0.0, float(body_collision_radius))
        if body_collision_strength is not None:
            self.body_collision_strength = max(0.0, float(body_collision_strength))
        if shake is not None:
            self.shaking = max(0.0, float(shake))
        if max_link_iterations is not None:
            self.max_link_iterations = max(0, int(max_link_iterations))

    def _anchor_matrix(self, index: int) -> List[List[float]]:
        transform = self.global_transforms[index]
        if self.use_offsets:
            return matrix_mul(transform, self.offsets[index])
        return transform

    def _anchor_position(self, index: int, transform: Optional[Sequence[Sequence[float]]] = None) -> Vector3:
        if transform is None:
            return matrix_translation(self._anchor_matrix(index))
        if self.use_offsets:
            return matrix_translation(matrix_mul(transform, self.offsets[index]))
        return matrix_translation(transform)

    def _pin_anchors(self) -> None:
        for index, distance in enumerate(self.distances):
            if distance <= 0.0:
                self.positions[index] = self._anchor_position(index)
                self.velocities[index] = (0.0, 0.0, 0.0)

    def _integrate_free_nodes(self, dt: float, wind: float, wind_vector: Vector3) -> None:
        wind_amount = max(0.0, min(1.0, float(wind)))
        wind_active = wind_amount > 0.0001 and _v_len(wind_vector) > 0.0001
        for index, position in enumerate(self.positions):
            if self.distances[index] <= 0.0:
                continue
            velocity = self.velocities[index]
            acceleration = (0.0, 0.0, -self._GRAVITY_ACCEL * self.masses[index] * self.gravity)
            if wind_active:
                r = _unit_noise(self._step_index, index, 0)
                acceleration = _v_add(acceleration, _v_mul(wind_vector, wind_amount * r * r * self._WIND_ACCEL))
            velocity = _v_add(velocity, _v_mul(acceleration, dt))
            if self.shaking > 0.0001 and self.shake_weights[index] > 0.0001:
                amount = self.shaking * self.shake_weights[index] * dt
                jitter = (
                    _unit_noise(self._step_index, index, 1) * 2.0 - 1.0,
                    _unit_noise(self._step_index, index, 2) * 2.0 - 1.0,
                    _unit_noise(self._step_index, index, 3) * 2.0 - 1.0,
                )
                velocity = _v_add(velocity, _v_mul(jitter, amount))
            self.velocities[index] = velocity
            self.positions[index] = _v_add(position, _v_mul(velocity, dt))

    def _project_anchor_limits(self) -> None:
        for index, distance in enumerate(self.distances):
            if distance <= 0.0:
                self.positions[index] = self._anchor_position(index)
                continue
            self._project_half_space(index)
            if self.use_offsets:
                self._project_offset_radius(index, distance)
            else:
                self._project_world_radius(index, distance)

    def _project_half_space(self, index: int) -> None:
        if not self.plane_collision:
            return
        transform = self.global_transforms[index]
        plane_origin = matrix_translation(transform)
        plane_axis = (float(transform[1][0]), float(transform[1][1]), float(transform[1][2]))
        penetration = _v_dot(plane_axis, _v_sub(self.positions[index], plane_origin))
        if penetration < 0.0:
            self.positions[index] = _v_add(self.positions[index], _v_mul(plane_axis, -penetration))

    def _project_world_radius(self, index: int, distance: float) -> None:
        center = matrix_translation(self.global_transforms[index])
        delta = _v_sub(self.positions[index], center)
        direction, length = _v_norm(delta)
        if length > distance > 0.0:
            self.positions[index] = _v_add(center, _v_mul(direction, distance))

    def _project_offset_radius(self, index: int, distance: float) -> None:
        anchor = self._anchor_matrix(index)
        center = matrix_translation(anchor)
        delta_world = _v_sub(self.positions[index], center)
        axes = (
            (float(anchor[0][0]), float(anchor[0][1]), float(anchor[0][2])),
            (float(anchor[1][0]), float(anchor[1][1]), float(anchor[1][2])),
            (float(anchor[2][0]), float(anchor[2][1]), float(anchor[2][2])),
        )
        local = []
        for axis in axes:
            denom = _v_dot(axis, axis)
            local.append(0.0 if abs(denom) <= 1e-12 else _v_dot(delta_world, axis) / denom)
        direction, length = _v_norm((local[0], local[1], local[2]))
        if length > distance > 0.0:
            self.positions[index] = transform_point(anchor, _v_mul(direction, distance))

    def _project_links(self) -> None:
        for index, (node_a, node_b) in enumerate(zip(self.link_a, self.link_b)):
            if not (0 <= node_a < len(self.positions) and 0 <= node_b < len(self.positions)):
                continue
            delta = _v_sub(self.positions[node_b], self.positions[node_a])
            direction, distance = _v_norm(delta)
            if distance <= 1e-12:
                continue
            rest_length = self.link_lengths[index]
            diff = distance - rest_length
            link_type = self.link_types[index]
            active = (
                (link_type == 0 and abs(diff) > 1e-12)
                or (link_type == 1 and diff < 0.0)
                or (link_type == 2 and diff > 0.0)
            )
            if not active:
                continue
            share_a, share_b = self._link_shares[index]
            if share_a <= 0.0 and share_b <= 0.0:
                continue
            correction = _v_mul(direction, diff)
            self.positions[node_a] = _v_add(self.positions[node_a], _v_mul(correction, share_a))
            self.positions[node_b] = _v_sub(self.positions[node_b], _v_mul(correction, share_b))

    def _project_body_colliders(self) -> None:
        if not self.body_collision:
            return
        radius_margin = max(0.0, float(self.body_collision_radius))
        strength = max(0.0, min(1.0, float(self.body_collision_strength)))
        if strength <= 0.0:
            return

        colliders: List[Tuple[Vector3, float]] = []
        for index, transform in enumerate(self.global_transforms):
            if self.distances[index] > 0.0:
                continue
            collider_matrix = matrix_mul(transform, self.offsets[index])
            radius = max(0.0, float(self.body_radii[index])) + radius_margin
            if radius > 1e-8:
                colliders.append((matrix_translation(collider_matrix), radius))
        if not colliders:
            return

        for index, position in enumerate(self.positions):
            if self.distances[index] <= 0.0:
                continue
            for center, radius in colliders:
                delta = _v_sub(position, center)
                direction, length = _v_norm(delta)
                if length >= radius:
                    continue
                if length <= 1e-8:
                    target = matrix_translation(self.global_transforms[index])
                    direction, length = _v_norm(_v_sub(target, center))
                    if length <= 1e-8:
                        direction = (1.0, 0.0, 0.0)
                position = _v_add(position, _v_mul(direction, (radius - length) * strength))
            self.positions[index] = position

    def _update_velocities(self, previous: Sequence[Vector3], dt: float) -> None:
        damping = max(0.0, float(self.dampening))
        if self.relaxed_mode:
            damping *= 0.9
        for index, (old_position, new_position) in enumerate(zip(previous, self.positions)):
            if self.distances[index] <= 0.0:
                self.velocities[index] = (0.0, 0.0, 0.0)
            else:
                self.velocities[index] = _v_mul(_v_div(_v_sub(new_position, old_position), dt), damping)

    def _rebuild_transforms(self) -> None:
        for index, transform in enumerate(self.global_transforms):
            position = self.positions[index]
            updated = matrix_with_translation(transform, position)
            lookat = self.lookats[index]
            if 0 <= lookat < len(self.positions) and self.orientation_radii[index] > 0.001:
                updated = _orient_x_axis_to(updated, _v_sub(self.positions[lookat], position), position)
            self.global_transforms[index] = updated
