from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field
from typing import Any, Mapping

from .dc_environment import (
    _array_children,
    _boolean,
    _children,
    _decode_value,
    _enum_text,
    _get_field,
    _integer,
    _number,
    _prop_name,
    embedded_resource_index,
    normalize_depot_path,
)


Vec4 = tuple[float, float, float, float]
Color = tuple[int, int, int, int]

_CURVE_KEY = struct.Struct("<ff4f4fii")


@dataclass(frozen=True)
class ParticleCurveKey:
    time: float
    value: float
    tangent_left: Vec4
    tangent_right: Vec4
    curve_type_l: int
    curve_type_r: int


@dataclass(frozen=True)
class ParticleCurve:
    keys: tuple[ParticleCurveKey, ...] = ()
    color: Color = (255, 255, 255, 255)
    base_type: str = "CT_Segmented"
    loop: bool = False


@dataclass(frozen=True)
class ParticleEvaluator:
    type_name: str
    value: Any = None
    minimum: Any = None
    maximum: Any = None
    start: Any = None
    end: Any = None
    curves: tuple[ParticleCurve, ...] = ()
    free_axes: str = ""
    spill: bool = True


@dataclass(frozen=True)
class ParticleModule:
    type_name: str
    enabled: bool = True
    properties: Mapping[str, Any] = field(default_factory=dict)
    evaluators: Mapping[str, ParticleEvaluator] = field(default_factory=dict)

    def property(self, name: str, default: Any = None) -> Any:
        return self.properties.get(name, default)

    def evaluator(self, name: str) -> ParticleEvaluator | None:
        return self.evaluators.get(name)


@dataclass(frozen=True)
class ParticleBurst:
    time: float = 0.0
    spawn_count: int = 1
    spawn_time_range: float = 0.0
    repeat_time: float = 0.0


@dataclass(frozen=True)
class ParticleLOD:
    birth_rate: ParticleEvaluator | None = None
    bursts: tuple[ParticleBurst, ...] = ()
    duration: float = 1.0
    duration_low: float = 0.0
    use_duration_range: bool = False
    delay: float = 0.0
    delay_low: float = 0.0
    use_delay_range: bool = False
    use_delay_once: bool = False
    sort_back_to_front: bool = False
    enabled: bool = True


@dataclass(frozen=True)
class ParticleMaterial:
    base_material: str = ""
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def parameter(self, name: str, default: Any = None) -> Any:
        return self.parameters.get(name, default)


@dataclass(frozen=True)
class ParticleEmitter:
    name: str = ""
    max_particles: int = 55
    loops: int = 0
    use_subframe_emission: bool = False
    keep_simulation_local: bool = False
    position_x: int = 0
    position_y: int = 0
    drawer_type: str = "CParticleDrawerBillboard"
    drawer_properties: Mapping[str, Any] = field(default_factory=dict)
    material: ParticleMaterial | None = None
    lods: tuple[ParticleLOD, ...] = field(default_factory=lambda: (ParticleLOD(),))
    modules: tuple[ParticleModule, ...] = ()

    def module(self, type_name: str) -> ParticleModule | None:
        return next((module for module in self.modules if module.type_name == type_name), None)

    def modules_of_type(self, type_name: str) -> tuple[ParticleModule, ...]:
        return tuple(module for module in self.modules if module.type_name == type_name)


@dataclass(frozen=True)
class ParticleSystem:
    source_path: str = ""
    auto_hide_distance: float = 100.0
    auto_hide_range: float = 0.0
    emitters: tuple[ParticleEmitter, ...] = ()

    def emitter(self, name: str) -> ParticleEmitter | None:
        return next((emitter for emitter in self.emitters if emitter.name == name), None)


_MODULE_DEFAULTS: Mapping[str, Mapping[str, Any]] = {
    "CParticleInitializerSpawnCircle": {
        "surfaceOnly": False,
        "worldSpace": True,
    },
    "CParticleInitializerVelocity": {"worldSpace": True},
    "CParticleModificatorAcceleration": {"worldSpace": True},
    "CParticleModificatorAlphaOverLife": {"modulate": True},
    "CParticleModificatorSizeOverLife": {"modulate": True},
    "CParticleModificatorTextureAnimation": {"animationMode": "TAM_Speed"},
}


def _chunk_type(chunk: Any) -> str:
    return str(getattr(chunk, "Type", "") or getattr(chunk, "name", "") or "")


def _properties(chunk: Any) -> dict[str, Any]:
    return {
        name: prop
        for prop in list(getattr(chunk, "PROPS", None) or _children(chunk))
        if (name := _prop_name(prop))
    }


def _value(prop: Any) -> Any:
    return _decode_value(prop, {}, "") if prop is not None else None


def _pointer_index(prop: Any) -> int | None:
    if prop is None:
        return None
    index = embedded_resource_index(prop)
    if index is not None:
        return index
    decoded = _value(prop)
    if isinstance(decoded, Mapping):
        try:
            index = int(decoded.get("chunk_index"))
        except (TypeError, ValueError):
            index = None
        if index is not None and index >= 0:
            return index
    raw = getattr(prop, "Value", None)
    try:
        pointer = int(raw)
    except (TypeError, ValueError):
        return None
    return pointer - 1 if 0 < pointer < 0xFFFFFFFF else None


def _exports(cr2w: Any) -> list[Any]:
    return list(getattr(cr2w, "CR2WExport", None) or [])


def _chunks(cr2w: Any) -> list[Any]:
    container = getattr(cr2w, "CHUNKS", None)
    return list(getattr(container, "CHUNKS", None) or [])


def _child_indices(cr2w: Any, parent_index: int) -> list[int]:
    parent_id = parent_index + 1
    return [
        index
        for index, export in enumerate(_exports(cr2w))
        if int(getattr(export, "parentID", 0) or 0) == parent_id
    ]


def _color(value: Any, default: Color = (255, 255, 255, 255)) -> Color:
    decoded = _value(value)
    if isinstance(decoded, Mapping):
        names = ("Red", "Green", "Blue", "Alpha")
        return tuple(int(decoded.get(name, default[index])) for index, name in enumerate(names))  # type: ignore[return-value]
    if isinstance(decoded, (tuple, list)):
        values = list(decoded[:4])
        values.extend(default[len(values):])
        return tuple(int(item) for item in values)  # type: ignore[return-value]
    return default


def _decode_curve(cr2w: Any, index: int, raw_data: bytes) -> ParticleCurve:
    chunks = _chunks(cr2w)
    exports = _exports(cr2w)
    chunk = chunks[index]
    export = exports[index]
    props = _properties(chunk)

    file_start = int(getattr(cr2w, "start", 0) or 0)
    chunk_start = file_start + int(getattr(export, "dataOffset", 0) or 0)
    chunk_end = chunk_start + int(getattr(export, "dataSize", 0) or 0)
    property_end = max(
        (int(getattr(prop, "dataEnd", chunk_start) or chunk_start) for prop in props.values()),
        default=chunk_start,
    )
    lower = max(chunk_start, property_end)

    count_offset = None
    last_offset = min(lower + 8, chunk_end - 4)
    for candidate in range(lower, last_offset + 1):
        if candidate < 0 or candidate + 4 > len(raw_data):
            continue
        count = struct.unpack_from("<I", raw_data, candidate)[0]
        if candidate + 4 + count * _CURVE_KEY.size == chunk_end:
            count_offset = candidate
            break
    if count_offset is None or chunk_end > len(raw_data):
        raise ValueError(f"Malformed CCurve chunk at export {index}")

    count = struct.unpack_from("<I", raw_data, count_offset)[0]
    keys: list[ParticleCurveKey] = []
    offset = count_offset + 4
    for _ in range(count):
        unpacked = _CURVE_KEY.unpack_from(raw_data, offset)
        keys.append(
            ParticleCurveKey(
                time=unpacked[0],
                value=unpacked[1],
                tangent_left=tuple(unpacked[2:6]),  # type: ignore[arg-type]
                tangent_right=tuple(unpacked[6:10]),  # type: ignore[arg-type]
                curve_type_l=unpacked[10],
                curve_type_r=unpacked[11],
            )
        )
        offset += _CURVE_KEY.size

    base_prop = _get_field(chunk, "dataBaseType", "baseType")
    loop_prop = _get_field(chunk, "loop", "dataLoop")
    return ParticleCurve(
        keys=tuple(keys),
        color=_color(props.get("color")),
        base_type=_enum_text(base_prop, "CT_Segmented"),
        loop=_boolean(loop_prop, False) if loop_prop is not None else False,
    )


def _evaluator_default(type_name: str, high: bool = False) -> Any:
    if type_name.startswith("CEvaluatorFloat"):
        return 1.0 if high else 0.0
    if type_name.startswith("CEvaluatorVector"):
        fill = 1.0 if high else 0.0
        return (fill, fill, fill, fill)
    if type_name.startswith("CEvaluatorColor"):
        return (255, 255, 255, 255) if high else (0, 0, 0, 0)
    return None


def _decode_evaluator(cr2w: Any, index: int, raw_data: bytes) -> ParticleEvaluator:
    chunks = _chunks(cr2w)
    chunk = chunks[index]
    type_name = _chunk_type(chunk)
    props = _properties(chunk)

    def field_value(serialized_name: str, default: Any) -> Any:
        prop = props.get(serialized_name)
        return _value(prop) if prop is not None else default

    curves = tuple(
        _decode_curve(cr2w, child_index, raw_data)
        for child_index in _child_indices(cr2w, index)
        if _chunk_type(chunks[child_index]) == "CCurve"
    )
    vector = type_name.startswith("CEvaluatorVector")
    is_const = type_name.endswith("Const") or type_name == "CEvaluatorColorRandom"
    is_random = type_name.endswith("RandomUniform")
    is_start_end = type_name.endswith("StartEnd")
    return ParticleEvaluator(
        type_name=type_name,
        value=field_value("value", _evaluator_default(type_name)) if is_const else None,
        minimum=field_value("min", _evaluator_default(type_name)) if is_random else None,
        maximum=field_value("max", _evaluator_default(type_name, True)) if is_random else None,
        start=field_value("start", _evaluator_default(type_name)) if is_start_end else None,
        end=field_value("end", _evaluator_default(type_name, True)) if is_start_end else None,
        curves=curves,
        free_axes=_enum_text(props.get("freeAxes"), "FVA_Three") if vector else "",
        spill=_boolean(props.get("spill"), True),
    )


def _decode_module(cr2w: Any, index: int, raw_data: bytes) -> ParticleModule:
    chunks = _chunks(cr2w)
    chunk = chunks[index]
    type_name = _chunk_type(chunk)
    props = _properties(chunk)
    properties = dict(_MODULE_DEFAULTS.get(type_name, {}))
    evaluators: dict[str, ParticleEvaluator] = {}

    for name, prop in props.items():
        target_index = _pointer_index(prop)
        if target_index is not None and 0 <= target_index < len(chunks):
            target_type = _chunk_type(chunks[target_index])
            if target_type.startswith("CEvaluator"):
                evaluators[name] = _decode_evaluator(cr2w, target_index, raw_data)
                continue
        if name not in {"editorColor", "editorGroup", "editorName", "isEnabled"}:
            properties[name] = _value(prop)

    return ParticleModule(
        type_name=type_name,
        enabled=_boolean(props.get("isEnabled"), True),
        properties=properties,
        evaluators=evaluators,
    )


def _struct_array_groups(prop: Any) -> list[list[Any]]:
    items = _array_children(prop)
    if not items:
        return []
    count = int(getattr(prop, "Count", 0) or 0)
    if count <= 1 and any(_prop_name(item) for item in items):
        return [items]
    groups = [_children(item) for item in items]
    groups = [group for group in groups if group]
    return groups or [items]


def _decode_bursts(prop: Any) -> tuple[ParticleBurst, ...]:
    bursts: list[ParticleBurst] = []
    for group in _struct_array_groups(prop):
        fields = {_prop_name(item): item for item in group if _prop_name(item)}
        bursts.append(
            ParticleBurst(
                time=_number(fields.get("burstTime"), 0.0),
                spawn_count=_integer(fields.get("spawnCount"), 1),
                spawn_time_range=_number(fields.get("spawnTimeRange"), 0.0),
                repeat_time=_number(fields.get("repeatTime"), 0.0),
            )
        )
    count = int(getattr(prop, "Count", 0) or 0)
    bursts.extend(ParticleBurst() for _ in range(max(0, count - len(bursts))))
    return tuple(bursts)


def _decode_lods(
    cr2w: Any,
    prop: Any,
    raw_data: bytes,
    fallback_birth_indices: tuple[int, ...] = (),
) -> tuple[ParticleLOD, ...]:
    chunks = _chunks(cr2w)
    lods: list[ParticleLOD] = []
    groups = _struct_array_groups(prop)
    if not groups and fallback_birth_indices:
        return (
            ParticleLOD(
                birth_rate=_decode_evaluator(
                    cr2w,
                    fallback_birth_indices[0],
                    raw_data,
                )
            ),
        )
    for lod_index, group in enumerate(groups):
        fields = {_prop_name(item): item for item in group if _prop_name(item)}
        duration = _get_field(fields.get("emitterDurationSettings"), "emitterDuration")
        duration_low = _get_field(fields.get("emitterDurationSettings"), "emitterDurationLow")
        duration_range = _get_field(fields.get("emitterDurationSettings"), "useEmitterDurationRange")
        delay = _get_field(fields.get("emitterDelaySettings"), "emitterDelay")
        delay_low = _get_field(fields.get("emitterDelaySettings"), "emitterDelayLow")
        delay_range = _get_field(fields.get("emitterDelaySettings"), "useEmitterDelayRange")
        delay_once = _get_field(fields.get("emitterDelaySettings"), "useEmitterDelayOnce")

        birth_rate = None
        birth_index = _pointer_index(fields.get("birthRate"))
        if birth_index is not None and 0 <= birth_index < len(chunks):
            if _chunk_type(chunks[birth_index]).startswith("CEvaluator"):
                birth_rate = _decode_evaluator(cr2w, birth_index, raw_data)
        if birth_rate is None and lod_index < len(fallback_birth_indices):
            birth_rate = _decode_evaluator(
                cr2w,
                fallback_birth_indices[lod_index],
                raw_data,
            )

        lods.append(
            ParticleLOD(
                birth_rate=birth_rate,
                bursts=_decode_bursts(fields.get("burstList")),
                duration=_number(duration, 1.0),
                duration_low=_number(duration_low, 0.0),
                use_duration_range=_boolean(duration_range, False),
                delay=_number(delay, 0.0),
                delay_low=_number(delay_low, 0.0),
                use_delay_range=_boolean(delay_range, False),
                use_delay_once=_boolean(delay_once, False),
                sort_back_to_front=_boolean(fields.get("sortBackToFront"), False),
                enabled=_boolean(fields.get("isEnabled"), True),
            )
        )
    return tuple(lods) or (ParticleLOD(),)


def _decode_material_chunk(chunk: Any) -> ParticleMaterial:
    props = _properties(chunk)
    base_material = _value(props.get("baseMaterial")) or ""
    parameters: dict[str, Any] = {}
    instance = getattr(chunk, "CMaterialInstance", None)
    parameter_array = getattr(instance, "InstanceParameters", None)
    for element in list(getattr(parameter_array, "elements", None) or []):
        prop = getattr(element, "PROP", element)
        name = _prop_name(prop)
        if name:
            parameters[name] = _value(prop)
    return ParticleMaterial(
        base_material=normalize_depot_path(base_material),
        parameters=parameters,
    )


def _decode_emitter(cr2w: Any, index: int, raw_data: bytes) -> ParticleEmitter:
    chunks = _chunks(cr2w)
    chunk = chunks[index]
    props = _properties(chunk)

    child_indices = _child_indices(cr2w, index)
    modules = tuple(
        _decode_module(cr2w, child_index, raw_data)
        for child_index in child_indices
        if _chunk_type(chunks[child_index]).startswith(
            ("CParticleInitializer", "CParticleModificator")
        )
    )
    birth_indices = tuple(
        child_index
        for child_index in child_indices
        if _chunk_type(chunks[child_index]).startswith("CEvaluatorFloat")
    )

    material = None
    material_prop = props.get("material")
    material_index = _pointer_index(material_prop)
    if material_index is not None and 0 <= material_index < len(chunks):
        if _chunk_type(chunks[material_index]) == "CMaterialInstance":
            material = _decode_material_chunk(chunks[material_index])
    elif isinstance(_value(material_prop), str):
        material = ParticleMaterial(base_material=normalize_depot_path(_value(material_prop)))

    drawer_type = "CParticleDrawerBillboard"
    drawer_properties: dict[str, Any] = {}
    drawer_index = _pointer_index(props.get("particleDrawer"))
    if drawer_index is not None and 0 <= drawer_index < len(chunks):
        drawer = chunks[drawer_index]
        drawer_type = _chunk_type(drawer) or drawer_type
        drawer_properties = {
            name: _value(prop)
            for name, prop in _properties(drawer).items()
        }

    return ParticleEmitter(
        name=str(_value(props.get("editorName")) or ""),
        max_particles=_integer(props.get("maxParticles"), 55),
        loops=_integer(props.get("emitterLoops"), 0),
        use_subframe_emission=_boolean(props.get("useSubFrameEmission"), False),
        keep_simulation_local=_boolean(props.get("keepSimulationLocal"), False),
        position_x=_integer(props.get("positionX"), 0),
        position_y=_integer(props.get("positionY"), 0),
        drawer_type=drawer_type,
        drawer_properties=drawer_properties,
        material=material,
        lods=_decode_lods(cr2w, props.get("lods"), raw_data, birth_indices),
        modules=modules,
    )


def _decode_particle(cr2w: Any, source_path: str, raw_data: bytes) -> ParticleSystem:
    chunks = _chunks(cr2w)
    system_index = next(
        (index for index, chunk in enumerate(chunks) if _chunk_type(chunk) == "CParticleSystem"),
        None,
    )
    if system_index is None:
        raise ValueError(f"{source_path!r} does not contain a CParticleSystem")
    props = _properties(chunks[system_index])
    emitters = tuple(
        _decode_emitter(cr2w, child_index, raw_data)
        for child_index in _child_indices(cr2w, system_index)
        if _chunk_type(chunks[child_index]) == "CParticleEmitter"
    )
    return ParticleSystem(
        source_path=os.path.abspath(source_path),
        auto_hide_distance=_number(props.get("autoHideDistance"), 100.0),
        auto_hide_range=_number(props.get("autoHideRange"), 0.0),
        emitters=emitters,
    )


def load_bin_particle(path: str | os.PathLike[str]) -> ParticleSystem:
    """Load a binary ``.w2p`` resource into Blender-independent data classes."""

    source_path = os.path.abspath(os.fspath(path))
    with open(source_path, "rb") as stream:
        raw_data = stream.read()
    from .CR2W_file import read_CR2W

    return _decode_particle(read_CR2W(source_path), source_path, raw_data)


__all__ = [
    "Color",
    "ParticleBurst",
    "ParticleCurve",
    "ParticleCurveKey",
    "ParticleEmitter",
    "ParticleEvaluator",
    "ParticleLOD",
    "ParticleMaterial",
    "ParticleModule",
    "ParticleSystem",
    "Vec4",
    "load_bin_particle",
]
