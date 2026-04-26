import io
import logging
import os
from pathlib import Path
from types import SimpleNamespace

from . import CR2W_types
from .CR2W_helpers import Enums
from .Types.VariousTypes import CBufferVLQInt32
from .bin_helpers import (
    ReadBit6,
    ReadVLQInt32,
    readFloat,
    readInt32,
    readSByte,
    readU32,
    readU32Check,
    readU64,
    readUShort,
    readUShortCheck,
)

log = logging.getLogger(__name__)

_CR2W_MAGIC = 1462915651
_UPDATED_RESOURCE_FORMAT_VERSION = int(getattr(CR2W_types, "UPDATED_RESOURCE_FORMAT_VERSION", 0) or 0)
_ENTITY_TYPES = frozenset(getattr(CR2W_types, "Entity_Type_List", ()) or ())
_DIRECT_COMPONENT_TYPES = frozenset(
    {
        "CMeshComponent",
        "CStaticMeshComponent",
        "CPointLightComponent",
        "CSpotLightComponent",
    }
)
_STREAM_MESH_COMPONENT_TYPES = frozenset(
    {
        "CStaticMeshComponent",
        "CMeshComponent",
        "CRigidMeshComponent",
        "CBgMeshComponent",
        "CBgNpcItemComponent",
        "CBoatBodyComponent",
        "CDressMeshComponent",
        "CFurComponent",
        "CImpostorMeshComponent",
        "CMergedMeshComponent",
        "CMergedShadowMeshComponent",
        "CMorphedMeshComponent",
        "CNavmeshComponent",
        "CRigidMeshComponentCooked",
        "CScriptedDestroyableComponent",
        "CWindowComponent",
    }
)
_STREAM_COMPONENT_TYPES = _STREAM_MESH_COMPONENT_TYPES | {"CClothComponent"}
_TARGET_PROP_NAMES = frozenset(
    {
        "actionName",
        "includes",
        "mesh",
        "name",
        "resource",
        "streamingDataBuffer",
        "streamingDistance",
        "template",
        "transform",
    }
)
_PLAN_ITEM_EXTRA_KEYS = frozenset(
    {
        "brightness",
        "radius",
        "color",
        "inner_angle",
        "outer_angle",
        "softness",
        "streaming_distance",
        "is_proxy_mesh",
        "proxy_role",
        "sector_flags",
    }
)
_SECTOR_FLAG_MESH_PART_OF_ENTITY_PROXY = 1 << 10
_SECTOR_FLAG_MESH_ROOT_ENTITY_PROXY = 1 << 11


def _path_indicates_proxy_mesh(repo_path, name=""):
    text = f"{repo_path or ''}/{name or ''}".replace("\\", "/").lower()
    return bool(text and "proxy" in text)


def _sector_proxy_role_from_flags(flags):
    try:
        value = int(flags or 0)
    except Exception:
        value = 0
    if value & _SECTOR_FLAG_MESH_ROOT_ENTITY_PROXY:
        return "root"
    if value & _SECTOR_FLAG_MESH_PART_OF_ENTITY_PROXY:
        return "part"
    return ""


def _light_color_to_dict(color):
    if color is None:
        return None
    try:
        return {
            "Red": float(getattr(color, "Red", 255.0) or 0.0),
            "Green": float(getattr(color, "Green", 255.0) or 0.0),
            "Blue": float(getattr(color, "Blue", 255.0) or 0.0),
        }
    except Exception:
        return None


def scan_cache_entry(
    level_path,
    resolved_path,
    file_mtime,
    file_size,
    *,
    dependency_resolver=None,
    dependency_loader=None,
):
    scan_result = scan_dependency_file(
        resolved_path,
        dependency_resolver=dependency_resolver,
        dependency_loader=dependency_loader,
    )
    if scan_result is None:
        return None
    return _build_cache_entry(level_path, resolved_path, file_mtime, file_size, scan_result)


def scan_dependency_file(
    resolved_path,
    *,
    dependency_resolver=None,
    dependency_loader=None,
):
    path_value = str(resolved_path or "").strip()
    if not path_value or not os.path.isfile(path_value):
        return None

    try:
        with open(path_value, "rb") as handle:
            cr2w_file = CR2W_types.getCR2W(handle, do_read_chunks=False)
            if not _supports_fast_scan(cr2w_file):
                return None
            return _scan_cr2w_structure(
                cr2w_file,
                handle,
                path_value,
                dependency_resolver=dependency_resolver,
                dependency_loader=dependency_loader,
                stream_only=False,
            )
    except Exception as exc:
        log.debug("Fast cache scan failed for %s: %s", path_value, exc)
        return None


def _supports_fast_scan(cr2w_file):
    header = getattr(cr2w_file, "HEADER", None)
    version = int(getattr(header, "version", 0) or 0)
    if version <= 115:
        return False
    if _UPDATED_RESOURCE_FORMAT_VERSION and version < _UPDATED_RESOURCE_FORMAT_VERSION:
        return False
    return True


def _new_scan_result():
    return {
        "entities": [],
        "includes": [],
        "sector_items": [],
        "foliage_items": [],
        "bounds_markers": [],
    }


def _merge_scan_result(target, source):
    if not source:
        return
    target["entities"].extend(list(source.get("entities", []) or []))
    target["includes"].extend(list(source.get("includes", []) or []))
    target["sector_items"].extend(list(source.get("sector_items", []) or []))
    target["foliage_items"].extend(list(source.get("foliage_items", []) or []))
    target["bounds_markers"].extend(list(source.get("bounds_markers", []) or []))


def _scan_cr2w_structure(
    cr2w_file,
    handle,
    source_name,
    *,
    dependency_resolver=None,
    dependency_loader=None,
    stream_only=False,
):
    result = _new_scan_result()
    pending_entities = []
    component_map = {}

    exports = list(getattr(cr2w_file, "CR2WExport", []) or [])
    for export_index, export in enumerate(exports):
        export_name = str(getattr(export, "name", "") or "").strip()
        if not export_name:
            continue

        export_info = _open_export(cr2w_file, handle, export_index)
        if export_info is None:
            return None
        class_start, class_end = export_info

        if export_name == "CSectorData" and not stream_only:
            sector_scan = _scan_sector_export(cr2w_file, handle, class_end)
            if sector_scan is None:
                return None
            result["sector_items"].extend(sector_scan["items"])
            result["bounds_markers"].extend(sector_scan["bounds_markers"])
            continue

        if export_name == "CFoliageResource" and not stream_only:
            foliage_scan = _scan_foliage_export(cr2w_file, handle, class_end)
            if foliage_scan is None:
                return None
            result["foliage_items"].extend(foliage_scan["items"])
            result["bounds_markers"].extend(foliage_scan["bounds_markers"])
            continue

        if export_name == "CEntityTemplate" and not stream_only:
            template_scan = _scan_template_export(
                cr2w_file,
                handle,
                class_end,
                source_name,
                dependency_resolver=dependency_resolver,
                dependency_loader=dependency_loader,
            )
            if template_scan is None:
                return None
            _merge_scan_result(result, template_scan)
            continue

        if export_name in _ENTITY_TYPES and not stream_only:
            entity_scan = _scan_entity_export(
                cr2w_file,
                handle,
                export_name,
                class_start,
                class_end,
                source_name,
                dependency_resolver=dependency_resolver,
                dependency_loader=dependency_loader,
            )
            if entity_scan is None:
                return None
            pending_entities.append(entity_scan)
            marker = _bounds_marker_from_transform(
                entity_scan.get("transform"),
                entity_scan.get("streaming_distance", 0.0),
            )
            if marker is not None:
                result["bounds_markers"].append(marker)
            continue

        if stream_only:
            if export_name in _STREAM_COMPONENT_TYPES:
                stream_item = _scan_component_export(
                    cr2w_file,
                    handle,
                    export_name,
                    class_end,
                    as_stream=True,
                )
                if stream_item is None:
                    continue
                result["sector_items"].append(stream_item)
            continue

        if export_name in _DIRECT_COMPONENT_TYPES:
            component_desc = _scan_component_export(
                cr2w_file,
                handle,
                export_name,
                class_end,
                as_stream=False,
            )
            if component_desc is None:
                continue
            component_map[export_index] = component_desc
            marker = _bounds_marker_from_transform(
                component_desc.get("transform"),
                component_desc.get("streaming_distance", 0.0),
            )
            if marker is not None:
                result["bounds_markers"].append(marker)

    for entity in pending_entities:
        components = []
        for component_index in list(entity.get("component_indices", []) or []):
            actual_index = int(component_index or 0) - 1
            if actual_index < 0:
                continue
            component_desc = component_map.get(actual_index)
            if component_desc is not None:
                components.append(component_desc)
        entity["components"] = components
        entity.pop("component_indices", None)
        result["entities"].append(entity)

    return result


def _open_export(cr2w_file, handle, export_index):
    exports = list(getattr(cr2w_file, "CR2WExport", []) or [])
    if export_index < 0 or export_index >= len(exports):
        return None

    export = exports[export_index]
    handle.seek(int(getattr(export, "dataOffset", 0) or 0) + int(getattr(cr2w_file, "start", 0) or 0))
    zero = readSByte(handle)
    if zero != 0:
        if zero == 1:
            _ = readInt32(handle)
        elif zero == -128:
            _ = ReadBit6(handle)
    class_start = handle.tell() - 1
    class_end = class_start + int(getattr(export, "dataSize", 0) or 0)
    if class_end < class_start:
        return None
    return class_start, class_end


def _scan_selected_props(cr2w_file, handle, class_end):
    values = {}
    while handle.tell() + 4 <= class_end:
        prop_offset = handle.tell()
        try:
            prop = CR2W_types.PROPSTART(handle, cr2w_file, SimpleNamespace())
        except Exception:
            handle.seek(prop_offset)
            break
        if getattr(prop, "type", None) is None or getattr(prop, "name", None) is None:
            handle.seek(prop_offset)
            break
        data_end = handle.tell() + int(getattr(prop, "size", 0) or 0) - 4
        if data_end < handle.tell() or data_end > class_end:
            handle.seek(prop_offset)
            break
        prop_name = str(getattr(prop, "name", "") or "").strip()
        if prop_name in _TARGET_PROP_NAMES:
            parsed_value = _parse_selected_prop_value(cr2w_file, handle, prop, data_end)
            if parsed_value is not None:
                values[prop_name] = parsed_value
        handle.seek(data_end)
    return values


def _parse_selected_prop_value(cr2w_file, handle, prop, data_end):
    prop_name = str(getattr(prop, "name", "") or "").strip()
    prop_type = str(getattr(prop, "type", "") or "").strip()
    count, element_type = _array_count_and_type(handle, data_end, prop_type)

    if prop_name in {"name", "actionName"}:
        if "String" in prop_type or element_type in {"String", "NodeRef", "LocalizedString"}:
            return _read_cstring_value(handle)
        if element_type == "CName":
            return _read_cname_value(handle, cr2w_file)
        return None

    if prop_name == "transform" and element_type == "EngineTransform":
        return _copy_engine_transform(CR2W_types.EngineTransform(handle))

    if prop_name == "template":
        return _read_first_handle_path(handle, cr2w_file, count)

    if prop_name == "includes":
        return _read_handle_paths(handle, cr2w_file, count)

    if prop_name in {"mesh", "resource"}:
        return _read_first_handle_path(handle, cr2w_file, count)

    if prop_name == "streamingDistance":
        if element_type == "Float":
            return float(readFloat(handle))
        if "Uint" in element_type:
            if element_type == "Uint16":
                return float(readUShort(handle))
            return float(readU32(handle))
        return None

    if prop_name == "streamingDataBuffer":
        if prop_type == "SharedDataBuffer":
            buffer_data = CR2W_types.CByteArray().Read(handle, 0)
            return {
                "bytes": bytes(getattr(buffer_data, "Bytes", b"") or b""),
                "buffer_index": 0,
            }
        if "ataBuffer" in prop_type:
            buffer_index = 0
            if int(getattr(prop, "size", 0) or 0) in {6, 8}:
                buffer_index = int(readUShort(handle) or 0)
            return {
                "bytes": b"",
                "buffer_index": buffer_index,
            }
        return None

    return None


def _array_count_and_type(handle, data_end, prop_type):
    count = 1
    the_type = str(prop_type or "")
    array_data_type = ""
    if "array" in the_type or "static:" in the_type or "curveData" in the_type or "]" in the_type:
        if ":" in the_type:
            delim = the_type.find(":")
            array_data_type = the_type[0:delim]
        else:
            delim = the_type.find("]")
            array_data_type = the_type[delim + 1 : len(the_type)]
        array_type = the_type[delim + 1 : len(the_type)]
        the_type = array_type
        if (
            handle.tell() + 2 < data_end
            and the_type != "inkWidgetLibraryItem"
            and readU32Check(handle, handle.tell()) != 0
            and (readU32Check(handle, handle.tell()) + handle.tell()) < data_end
        ):
            if readUShortCheck(handle, handle.tell()) == 0:
                handle.seek(2, 1)
                count = readUShort(handle)
            else:
                count = int(readU32Check(handle, handle.tell()) or 0)
                handle.seek(4, 1)
        elif (
            array_data_type == "array"
            and handle.tell() + 4 <= data_end
            and (data_end - handle.tell()) == 4
            and readU32Check(handle, handle.tell()) == 0
        ):
            count = 0
            handle.seek(4, 1)

    element_type = the_type
    if array_data_type == "array" and "," in the_type:
        element_type = the_type.rsplit(",", 1)[-1]
    return int(count or 0), str(element_type or "")


def _read_first_handle_path(handle, cr2w_file, count):
    paths = _read_handle_paths(handle, cr2w_file, count)
    return paths[0] if paths else ""


def _read_handle_paths(handle, cr2w_file, count):
    paths = []
    for _ in range(max(0, int(count or 0))):
        depot_path = _read_handle_path(handle, cr2w_file)
        if depot_path:
            paths.append(depot_path)
    return paths


def _read_handle_path(handle, cr2w_file):
    val = readInt32(handle)
    if val >= 0:
        return ""
    import_index = (-val) - 1
    imports = list(getattr(cr2w_file, "CR2WImport", []) or [])
    if 0 <= import_index < len(imports):
        return str(getattr(imports[import_index], "path", "") or "").strip()
    return ""


def _read_cname_value(handle, cr2w_file):
    index = int(readUShort(handle) or 0)
    cnames = list(getattr(cr2w_file, "CNAMES", []) or [])
    if 0 <= index < len(cnames):
        try:
            return str(cnames[index].name.value or "")
        except Exception:
            return ""
    return ""


def _read_cstring_value(handle):
    try:
        return str(CR2W_types.CSTRING(handle).String or "")
    except Exception:
        return ""


def _scan_sector_export(cr2w_file, handle, class_end):
    _ = _scan_selected_props(cr2w_file, handle, class_end)
    if handle.tell() >= class_end:
        return {"items": [], "bounds_markers": []}

    handle.seek(-1, 1)
    _ = readU64(handle)

    resources = []
    resource_count = int(ReadBit6(handle) or 0)
    for _ in range(resource_count):
        resources.append(CR2W_types.CSectorDataResource(handle, cr2w_file, None))

    objects = []
    object_count = int(ReadBit6(handle) or 0)
    for _ in range(object_count):
        objects.append(CR2W_types.CSectorDataObject(handle, cr2w_file, None))

    block_size = int(ReadVLQInt32(handle) or 0)
    block_data = []
    for index, obj in enumerate(objects):
        current_offset = int(getattr(obj, "offset", 0) or 0)
        if index < len(objects) - 1:
            next_offset = int(getattr(objects[index + 1], "offset", 0) or 0)
            length = next_offset - current_offset
        else:
            length = block_size - current_offset
        if length <= 0:
            return None
        block_data.append(CR2W_types.SBlockData(handle, length, int(getattr(obj, "type", 0) or 0)))

    items = []
    bounds_markers = []
    for obj in objects:
        pos = getattr(obj, "position", None)
        if pos is None:
            continue
        bounds_markers.append(
            {
                "x": float(getattr(pos, "x", 0.0) or 0.0),
                "y": float(getattr(pos, "y", 0.0) or 0.0),
                "radius": max(0.0, float(getattr(obj, "radius", 0.0) or 0.0)),
            }
        )

    for block in block_data:
        packed_type = int(getattr(block, "packedObjectType", -1) or -1)
        item = None
        if packed_type in {
            Enums.BlockDataObjectType.Mesh,
            Enums.BlockDataObjectType.RigidBody,
            Enums.BlockDataObjectType.Collision,
        }:
            resource_index = int(getattr(block, "resourceIndex", -1) or -1)
            repo_path = ""
            if 0 <= resource_index < len(resources):
                repo_path = str(getattr(resources[resource_index], "pathHash", "") or "").strip()
            if repo_path:
                kind = "mesh"
                if packed_type == Enums.BlockDataObjectType.RigidBody:
                    kind = "rigid"
                elif packed_type == Enums.BlockDataObjectType.Collision:
                    kind = "collision"
                sector_flags = int(getattr(block, "flags", 0) or 0)
                proxy_role = _sector_proxy_role_from_flags(sector_flags) if kind == "mesh" else ""
                item = {
                    "kind": kind,
                    "name": Path(repo_path).stem or kind.title(),
                    "repo_path": repo_path,
                    "transform": None,
                    "matrix": _matrix3x3_to_rows(getattr(block, "rotationMatrix", None)),
                    "translation": _vector3_to_tuple(getattr(block, "position", None)),
                    "local_position": _vector3_to_tuple(getattr(block, "position", None)),
                    "sector_flags": sector_flags,
                }
                if kind == "mesh":
                    item["is_proxy_mesh"] = bool(proxy_role) or _path_indicates_proxy_mesh(repo_path)
                    if proxy_role:
                        item["proxy_role"] = proxy_role
        elif packed_type == Enums.BlockDataObjectType.PointLight:
            light = getattr(block, "packedObject", None)
            item = {
                "kind": "point_light",
                "name": "PointLight",
                "repo_path": "",
                "transform": None,
                "matrix": _matrix3x3_to_rows(getattr(block, "rotationMatrix", None)),
                "translation": _vector3_to_tuple(getattr(block, "position", None)),
                "local_position": _vector3_to_tuple(getattr(block, "position", None)),
                "color": _light_color_to_dict(getattr(light, "color", None)),
                "radius": float(getattr(light, "radius", 0.0) or 0.0),
                "brightness": float(getattr(light, "brightness", 1.0) or 0.0),
            }
        elif packed_type == Enums.BlockDataObjectType.SpotLight:
            light = getattr(block, "packedObject", None)
            item = {
                "kind": "spot_light",
                "name": "SpotLight",
                "repo_path": "",
                "transform": None,
                "matrix": _matrix3x3_to_rows(getattr(block, "rotationMatrix", None)),
                "translation": _vector3_to_tuple(getattr(block, "position", None)),
                "local_position": _vector3_to_tuple(getattr(block, "position", None)),
                "color": _light_color_to_dict(getattr(light, "color", None)),
                "radius": float(getattr(light, "radius", 0.0) or 0.0),
                "brightness": float(getattr(light, "brightness", 1.0) or 0.0),
                "inner_angle": float(getattr(light, "innerAngle", 0.0) or 0.0),
                "outer_angle": float(getattr(light, "outerAngle", 0.0) or 0.0),
                "softness": float(getattr(light, "softness", 0.0) or 0.0),
            }
        if item is not None:
            items.append(item)

    return {
        "items": items,
        "bounds_markers": bounds_markers,
    }


def _scan_foliage_export(cr2w_file, handle, class_end):
    _ = _scan_selected_props(cr2w_file, handle, class_end)
    trees = CBufferVLQInt32(cr2w_file, CR2W_types.SFoliageResourceData)
    trees.Read(handle, 0)
    grasses = CBufferVLQInt32(cr2w_file, CR2W_types.SFoliageResourceData)
    grasses.Read(handle, 0)

    items = []
    bounds_markers = []

    for tree_collection in list(getattr(trees, "elements", []) or []):
        repo_path = _handle_to_repo_path(getattr(tree_collection, "TreeType", None))
        for transform in list(getattr(getattr(tree_collection, "TreeCollection", None), "elements", []) or []):
            local_position = _foliage_transform_position(transform)
            if local_position is not None:
                bounds_markers.append({"x": local_position[0], "y": local_position[1], "radius": 0.0})
            items.append(
                {
                    "kind": "foliage",
                    "name": Path(repo_path).stem or "Foliage",
                    "repo_path": repo_path,
                    "transform": _copy_foliage_transform(transform),
                    "matrix": None,
                    "translation": None,
                    "local_position": local_position,
                }
            )

    for tree_collection in list(getattr(grasses, "elements", []) or []):
        repo_path = _handle_to_repo_path(getattr(tree_collection, "TreeType", None))
        for transform in list(getattr(getattr(tree_collection, "TreeCollection", None), "elements", []) or []):
            local_position = _foliage_transform_position(transform)
            if local_position is not None:
                bounds_markers.append({"x": local_position[0], "y": local_position[1], "radius": 0.0})
            items.append(
                {
                    "kind": "grass",
                    "name": Path(repo_path).stem or "Grass",
                    "repo_path": repo_path,
                    "transform": _copy_foliage_transform(transform),
                    "matrix": None,
                    "translation": None,
                    "local_position": local_position,
                }
            )

    return {
        "items": items,
        "bounds_markers": bounds_markers,
    }


def _scan_template_export(
    cr2w_file,
    handle,
    class_end,
    source_name,
    *,
    dependency_resolver=None,
    dependency_loader=None,
):
    props = _scan_selected_props(cr2w_file, handle, class_end)
    result = _new_scan_result()

    for include_path in list(props.get("includes", []) or []):
        resolved_path = _resolve_dependency_path(include_path, cr2w_file, dependency_resolver)
        if not resolved_path:
            continue
        dependency_scan = None
        if dependency_loader is not None:
            dependency_scan = dependency_loader(resolved_path)
        else:
            dependency_scan = scan_dependency_file(
                resolved_path,
                dependency_resolver=dependency_resolver,
                dependency_loader=dependency_loader,
            )
        if dependency_scan is None:
            return None
        if dependency_scan is not None:
            result["includes"].append(dependency_scan)

    embedded_scan = _scan_embedded_template_data(
        handle,
        class_end,
        f"{source_name}:flatCompiledData",
        dependency_resolver=dependency_resolver,
        dependency_loader=dependency_loader,
    )
    if embedded_scan is False:
        return None
    if embedded_scan is not None:
        _merge_scan_result(result, embedded_scan)
    return result


def _scan_entity_export(
    cr2w_file,
    handle,
    export_name,
    class_start,
    class_end,
    source_name,
    *,
    dependency_resolver=None,
    dependency_loader=None,
):
    props = _scan_selected_props(cr2w_file, handle, class_end)
    entity_name = str(props.get("name", "") or "").strip()
    if entity_name:
        entity_name = f"{entity_name} ({export_name})"
    else:
        entity_name = export_name

    transform = props.get("transform")
    streaming_buffer = props.get("streamingDataBuffer") or {}
    template_path = str(props.get("template", "") or "").strip()
    template_scan = None
    if template_path:
        resolved_template = _resolve_dependency_path(template_path, cr2w_file, dependency_resolver)
        if resolved_template:
            if dependency_loader is not None:
                template_scan = dependency_loader(resolved_template)
            else:
                template_scan = scan_dependency_file(
                    resolved_template,
                    dependency_resolver=dependency_resolver,
                    dependency_loader=dependency_loader,
                )
            if template_scan is None:
                return None

    stream_items = []
    buffer_bytes = _extract_buffer_bytes(cr2w_file, streaming_buffer)
    if buffer_bytes:
        stream_items = _scan_stream_buffer_items(buffer_bytes, f"{source_name}:{export_name}:stream")
        if stream_items is None:
            return None

    component_indices = []
    is_created_from_template = bool(template_path)
    handle.seek(min(class_end, handle.tell() + 10))
    size = class_end - class_start
    end_pos = handle.tell()
    bytes_left = size - (end_pos - class_start)
    if not is_created_from_template:
        handle.seek(min(class_end, handle.tell() + 63))
        if bytes_left > 0 and handle.tell() < class_end:
            try:
                element_count = int(ReadBit6(handle) or 0)
            except Exception:
                element_count = 0
            if 0 <= element_count < 300:
                for _ in range(element_count):
                    if handle.tell() + 4 > class_end:
                        break
                    component_indices.append(int(readInt32(handle) or 0))

    return {
        "name": entity_name,
        "type": export_name,
        "transform": transform,
        "template_path": template_path,
        "template": template_scan,
        "stream_items": list(stream_items or []),
        "component_indices": component_indices,
        "components": [],
        "streaming_distance": float(props.get("streamingDistance", 0.0) or 0.0),
    }


def _scan_component_export(cr2w_file, handle, export_name, class_end, *, as_stream):
    props = _scan_selected_props(cr2w_file, handle, class_end)
    transform = props.get("transform")
    local_position = _transform_position(transform)

    if as_stream:
        if export_name in _STREAM_MESH_COMPONENT_TYPES:
            repo_path = str(props.get("mesh", "") or props.get("resource", "") or "").strip()
            if not repo_path:
                return None
            return {
                "kind": "mesh",
                "name": Path(repo_path).stem or export_name,
                "repo_path": repo_path,
                "transform": transform,
                "matrix": None,
                "translation": None,
                "local_position": local_position,
                "is_proxy_mesh": _path_indicates_proxy_mesh(repo_path, export_name),
            }
        if export_name == "CClothComponent":
            repo_path = str(props.get("resource", "") or "").strip()
            if not repo_path:
                return None
            cloth_name = str(props.get("name", "") or "").strip() or Path(repo_path).stem or "Cloth"
            return {
                "kind": "cloth",
                "name": cloth_name,
                "repo_path": repo_path,
                "transform": transform,
                "matrix": None,
                "translation": None,
                "local_position": local_position,
            }
        return None

    if export_name in {"CMeshComponent", "CStaticMeshComponent"}:
        repo_path = str(props.get("mesh", "") or props.get("resource", "") or "").strip()
        if not repo_path:
            return None
        return {
            "kind": "component_mesh",
            "name": Path(repo_path).stem or export_name,
            "repo_path": repo_path,
            "transform": transform,
            "matrix": None,
            "translation": None,
            "local_position": local_position,
            "streaming_distance": float(props.get("streamingDistance", 0.0) or 0.0),
            "is_proxy_mesh": _path_indicates_proxy_mesh(repo_path, export_name),
        }

    if export_name == "CPointLightComponent":
        return {
            "kind": "component_point_light",
            "name": "PointLightComponent",
            "repo_path": "",
            "transform": transform,
            "matrix": None,
            "translation": None,
            "local_position": local_position,
            "streaming_distance": float(props.get("streamingDistance", 0.0) or 0.0),
        }

    if export_name == "CSpotLightComponent":
        return {
            "kind": "component_spot_light",
            "name": "SpotLightComponent",
            "repo_path": "",
            "transform": transform,
            "matrix": None,
            "translation": None,
            "local_position": local_position,
            "streaming_distance": float(props.get("streamingDistance", 0.0) or 0.0),
        }

    return None


def _resolve_dependency_path(depot_path, cr2w_file, dependency_resolver):
    depot_value = str(depot_path or "").strip()
    if not depot_value:
        return ""
    if dependency_resolver is not None:
        try:
            resolved = dependency_resolver(depot_value, getattr(getattr(cr2w_file, "HEADER", None), "version", 999))
        except TypeError:
            resolved = dependency_resolver(depot_value)
        if resolved:
            return str(resolved)
    return ""


def _scan_embedded_template_data(
    handle,
    class_end,
    source_name,
    *,
    dependency_resolver=None,
    dependency_loader=None,
):
    current_pos = handle.tell()
    if current_pos >= class_end:
        return None
    remaining = handle.read(class_end - current_pos)
    if not remaining:
        return None
    start_index = remaining.find(b"CR2W")
    if start_index < 0:
        return None
    stream = io.BytesIO(remaining[start_index:])
    stream.name = source_name
    try:
        cr2w_file = CR2W_types.getCR2W(stream, do_read_chunks=False)
    except Exception:
        return False
    if not _supports_fast_scan(cr2w_file):
        return False
    scan_result = _scan_cr2w_structure(
        cr2w_file,
        stream,
        source_name,
        dependency_resolver=dependency_resolver,
        dependency_loader=dependency_loader,
        stream_only=False,
    )
    if scan_result is None:
        return False
    return scan_result


def _scan_stream_buffer_items(buffer_bytes, source_name):
    if not buffer_bytes:
        return []
    data = bytes(buffer_bytes)
    start_index = data.find(b"CR2W")
    if start_index < 0:
        return []
    stream = io.BytesIO(data[start_index:])
    stream.name = source_name
    try:
        cr2w_file = CR2W_types.getCR2W(stream, do_read_chunks=False)
    except Exception:
        return None
    if not _supports_fast_scan(cr2w_file):
        return None
    stream_scan = _scan_cr2w_structure(cr2w_file, stream, source_name, stream_only=True)
    if stream_scan is None:
        return None
    return list(stream_scan.get("sector_items", []) or [])


def _extract_buffer_bytes(cr2w_file, streaming_buffer):
    if not isinstance(streaming_buffer, dict):
        return b""
    raw_bytes = streaming_buffer.get("bytes")
    if raw_bytes:
        return bytes(raw_bytes)
    buffer_index = int(streaming_buffer.get("buffer_index", 0) or 0) - 1
    buffer_data = list(getattr(cr2w_file, "BufferData", []) or [])
    if 0 <= buffer_index < len(buffer_data):
        return bytes(buffer_data[buffer_index] or b"")
    return b""


def _build_cache_entry(level_path, resolved_path, file_mtime, file_size, scan_result):
    items = []
    state = {
        "items": items,
        "next_id": 1,
    }

    for item_desc in list(scan_result.get("sector_items", []) or []):
        _append_item_from_desc(state, item_desc, parent_id="", parent_position=None)

    for item_desc in list(scan_result.get("foliage_items", []) or []):
        _append_item_from_desc(state, item_desc, parent_id="", parent_position=None)

    _append_scan_entities(state, scan_result, parent_id="", parent_position=None)

    bounds = _bounds_from_markers_and_items(scan_result.get("bounds_markers", []) or [], items)
    import_item_count = 0
    for item in items:
        if item.get("world_position") is not None:
            import_item_count += 1

    entry = {
        "level_path": level_path,
        "resolved_path": resolved_path,
        "file_mtime": file_mtime,
        "file_size": file_size,
        "has_bounds": bool(bounds is not None),
        "object_count": int(bounds.get("object_count", 0) if bounds is not None else 0),
        "has_manifest": True,
        "import_item_count": int(import_item_count),
        "items": items,
    }
    if bounds is not None:
        entry.update(bounds)
    return entry


def _append_scan_entities(state, scan_result, *, parent_id, parent_position):
    for include_scan in list(scan_result.get("includes", []) or []):
        _append_scan_entities(state, include_scan, parent_id=parent_id, parent_position=parent_position)
    for entity in list(scan_result.get("entities", []) or []):
        _append_entity(state, entity, parent_id=parent_id, parent_position=parent_position)


def _append_entity(state, entity, *, parent_id, parent_position):
    entity_transform = entity.get("transform")
    entity_position = _compose_world_position(_transform_position(entity_transform), parent_position)
    entity_repo_path = str(entity.get("template_path", "") or "").strip()
    entity_id = _append_plan_item(
        state,
        kind="entity",
        name=str(entity.get("name", "") or entity.get("type", "") or "Entity"),
        parent_id=parent_id,
        repo_path=entity_repo_path,
        transform=entity_transform,
        matrix=None,
        translation=None,
        world_position=entity_position,
    )
    item_count_before_children = len(state["items"])

    for item_desc in list(entity.get("stream_items", []) or []):
        _append_item_from_desc(state, item_desc, parent_id=entity_id, parent_position=entity_position)

    for item_desc in list(entity.get("components", []) or []):
        _append_item_from_desc(state, item_desc, parent_id=entity_id, parent_position=entity_position)

    template_scan = entity.get("template")
    if template_scan is not None:
        _append_scan_entities(state, template_scan, parent_id=entity_id, parent_position=entity_position)

    if len(state["items"]) == item_count_before_children:
        _remove_plan_item(state, entity_id)


def _append_item_from_desc(state, item_desc, *, parent_id, parent_position):
    local_position = item_desc.get("local_position")
    world_position = _compose_world_position(local_position, parent_position)
    item_id = _append_plan_item(
        state,
        kind=str(item_desc.get("kind", "") or "unknown"),
        name=str(item_desc.get("name", "") or item_desc.get("kind", "") or "Item"),
        parent_id=parent_id,
        repo_path=str(item_desc.get("repo_path", "") or ""),
        transform=item_desc.get("transform"),
        matrix=item_desc.get("matrix"),
        translation=item_desc.get("translation"),
        world_position=world_position,
    )
    if item_id and state.get("items"):
        item = state["items"][-1]
        for key in _PLAN_ITEM_EXTRA_KEYS:
            if key in item_desc:
                item[key] = item_desc.get(key)
    return item_id


def _append_plan_item(
    state,
    *,
    kind,
    name,
    parent_id,
    repo_path,
    transform,
    matrix,
    translation,
    world_position,
):
    item_id = f"item_{int(state['next_id'])}"
    state["next_id"] = int(state["next_id"]) + 1
    state["items"].append(
        {
            "id": item_id,
            "kind": str(kind or "unknown"),
            "name": str(name or kind or "Item"),
            "parent_id": str(parent_id or ""),
            "repo_path": str(repo_path or ""),
            "transform": _copy_transform_dict(transform),
            "matrix": _copy_matrix_rows(matrix),
            "translation": _copy_position(translation),
            "world_position": _copy_position(world_position),
        }
    )
    return item_id


def _remove_plan_item(state, item_id):
    items = state.get("items", [])
    for index, item in enumerate(items):
        if str(item.get("id", "") or "") == str(item_id or ""):
            items.pop(index)
            return


def _bounds_from_markers_and_items(markers, items):
    min_x = float("inf")
    min_y = float("inf")
    max_x = float("-inf")
    max_y = float("-inf")
    object_count = 0

    for marker in list(markers or []):
        try:
            x = float(marker.get("x", 0.0) or 0.0)
            y = float(marker.get("y", 0.0) or 0.0)
            radius = max(0.0, float(marker.get("radius", 0.0) or 0.0))
        except Exception:
            continue
        min_x = min(min_x, x - radius)
        min_y = min(min_y, y - radius)
        max_x = max(max_x, x + radius)
        max_y = max(max_y, y + radius)
        object_count += 1

    if object_count <= 0:
        for item in list(items or []):
            position = item.get("world_position")
            if not isinstance(position, (list, tuple)) or len(position) < 2:
                continue
            try:
                x = float(position[0])
                y = float(position[1])
            except Exception:
                continue
            min_x = min(min_x, x)
            min_y = min(min_y, y)
            max_x = max(max_x, x)
            max_y = max(max_y, y)
            object_count += 1

    if object_count <= 0:
        return None

    return {
        "has_bounds": True,
        "min_x": float(min_x),
        "min_y": float(min_y),
        "max_x": float(max_x),
        "max_y": float(max_y),
        "object_count": int(object_count),
    }


def _bounds_marker_from_transform(transform, radius):
    position = _transform_position(transform)
    if position is None:
        return None
    return {
        "x": float(position[0]),
        "y": float(position[1]),
        "radius": max(0.0, float(radius or 0.0)),
    }


def _handle_to_repo_path(handle_value):
    depot_path = getattr(handle_value, "DepotPath", None)
    return str(depot_path or "").strip()


def _foliage_transform_position(transform):
    if transform is None:
        return None
    try:
        return (
            float(getattr(transform, "X", 0.0) or 0.0),
            float(getattr(transform, "Y", 0.0) or 0.0),
            float(getattr(transform, "Z", 0.0) or 0.0),
        )
    except Exception:
        return None


def _copy_foliage_transform(transform):
    if transform is None:
        return None
    return {
        "X": float(getattr(transform, "X", 0.0) or 0.0),
        "Y": float(getattr(transform, "Y", 0.0) or 0.0),
        "Z": float(getattr(transform, "Z", 0.0) or 0.0),
        "Yaw": float(getattr(transform, "Yaw", 0.0) or 0.0),
        "Pitch": float(getattr(transform, "Pitch", 0.0) or 0.0),
        "Roll": float(getattr(transform, "Roll", 0.0) or 0.0),
        "Scale_x": 1.0,
        "Scale_y": 1.0,
        "Scale_z": 1.0,
    }


def _copy_engine_transform(transform):
    if transform is None:
        return None
    return {
        "X": float(getattr(transform, "X", 0.0) or 0.0),
        "Y": float(getattr(transform, "Y", 0.0) or 0.0),
        "Z": float(getattr(transform, "Z", 0.0) or 0.0),
        "Yaw": float(getattr(transform, "Yaw", 0.0) or 0.0),
        "Pitch": float(getattr(transform, "Pitch", 0.0) or 0.0),
        "Roll": float(getattr(transform, "Roll", 0.0) or 0.0),
        "Scale_x": float(getattr(transform, "Scale_x", 1.0) or 1.0),
        "Scale_y": float(getattr(transform, "Scale_y", 1.0) or 1.0),
        "Scale_z": float(getattr(transform, "Scale_z", 1.0) or 1.0),
    }


def _copy_transform_dict(transform):
    if not isinstance(transform, dict):
        return None
    return {
        "X": float(transform.get("X", 0.0) or 0.0),
        "Y": float(transform.get("Y", 0.0) or 0.0),
        "Z": float(transform.get("Z", 0.0) or 0.0),
        "Yaw": float(transform.get("Yaw", 0.0) or 0.0),
        "Pitch": float(transform.get("Pitch", 0.0) or 0.0),
        "Roll": float(transform.get("Roll", 0.0) or 0.0),
        "Scale_x": float(transform.get("Scale_x", 1.0) or 1.0),
        "Scale_y": float(transform.get("Scale_y", 1.0) or 1.0),
        "Scale_z": float(transform.get("Scale_z", 1.0) or 1.0),
    }


def _transform_position(transform):
    if not isinstance(transform, dict):
        return None
    return (
        float(transform.get("X", 0.0) or 0.0),
        float(transform.get("Y", 0.0) or 0.0),
        float(transform.get("Z", 0.0) or 0.0),
    )


def _vector3_to_tuple(value):
    if value is None:
        return None
    try:
        return (
            float(getattr(value, "x", 0.0) or 0.0),
            float(getattr(value, "y", 0.0) or 0.0),
            float(getattr(value, "z", 0.0) or 0.0),
        )
    except Exception:
        return None


def _matrix3x3_to_rows(matrix_value):
    if matrix_value is None:
        return None
    try:
        return (
            (
                float(getattr(matrix_value, "ax", 0.0) or 0.0),
                float(getattr(matrix_value, "ay", 0.0) or 0.0),
                float(getattr(matrix_value, "az", 0.0) or 0.0),
            ),
            (
                float(getattr(matrix_value, "bx", 0.0) or 0.0),
                float(getattr(matrix_value, "by", 0.0) or 0.0),
                float(getattr(matrix_value, "bz", 0.0) or 0.0),
            ),
            (
                float(getattr(matrix_value, "cx", 0.0) or 0.0),
                float(getattr(matrix_value, "cy", 0.0) or 0.0),
                float(getattr(matrix_value, "cz", 0.0) or 0.0),
            ),
        )
    except Exception:
        return None


def _compose_world_position(local_position, parent_position):
    if local_position is None:
        return _copy_position(parent_position)
    if parent_position is None:
        return _copy_position(local_position)
    return (
        float(parent_position[0]) + float(local_position[0]),
        float(parent_position[1]) + float(local_position[1]),
        float(parent_position[2]) + float(local_position[2]),
    )


def _copy_position(value):
    if value is None:
        return None
    try:
        return (
            float(value[0]),
            float(value[1]),
            float(value[2]),
        )
    except Exception:
        return None


def _copy_matrix_rows(value):
    if value is None:
        return None
    try:
        return tuple(tuple(float(cell) for cell in row) for row in value)
    except Exception:
        return None
