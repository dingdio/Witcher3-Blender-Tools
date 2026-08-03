"""Pure-Python APX XML parsing and import sanitization helpers."""

import hashlib
import logging
import os
from pathlib import Path
from typing import Dict, List, Tuple
from xml.etree import ElementTree

from ..extension_paths import get_temp_root


log = logging.getLogger(__name__)

__all__ = ["sanitize_apx_for_import"]


def _apx_find_child(
    root,
    tag: str,
    attr: str | None = None,
    attr_value: str | None = None,
):
    for elem in root:
        if elem.tag != tag:
            continue
        if attr is None or elem.attrib.get(attr) == attr_value:
            return elem
    raise LookupError(f"Missing APX element {tag} {attr}={attr_value}")


def _apx_try_find_child(
    root,
    tag: str,
    attr: str | None = None,
    attr_value: str | None = None,
):
    try:
        return _apx_find_child(root, tag, attr, attr_value)
    except LookupError:
        return None


def _parse_apx_int_array_text(text: str) -> List[int]:
    raw = str(text or "").replace(",", " ").split()
    return [int(value) for value in raw]


def _format_apx_int_array_text(values: List[int]) -> str:
    return " ".join(str(value) for value in values)


def _sanitize_apx_triangle_indices(
    indices: List[int],
    vertex_count: int | None = None,
    positions: List[Tuple[float, float, float]] | None = None,
) -> Tuple[List[int], Dict[str, int]]:
    stats = {
        "removed_total": 0,
        "removed_degenerate": 0,
        "removed_zero_area": 0,
        "removed_out_of_range": 0,
        "removed_truncated": 0,
    }
    if not indices:
        return indices, stats

    filtered: List[int] = []
    usable_count = len(indices) - (len(indices) % 3)
    if usable_count != len(indices):
        stats["removed_total"] += 1
        stats["removed_truncated"] += 1

    for start in range(0, usable_count, 3):
        tri = indices[start:start + 3]
        a, b, c = tri
        if len({a, b, c}) < 3:
            stats["removed_total"] += 1
            stats["removed_degenerate"] += 1
            continue
        if vertex_count is not None and (
            a < 0 or b < 0 or c < 0 or
            a >= vertex_count or b >= vertex_count or c >= vertex_count
        ):
            stats["removed_total"] += 1
            stats["removed_out_of_range"] += 1
            continue
        if positions is not None:
            try:
                pa, pb, pc = positions[a], positions[b], positions[c]
                ux, uy, uz = (
                    pb[0] - pa[0],
                    pb[1] - pa[1],
                    pb[2] - pa[2],
                )
                vx, vy, vz = (
                    pc[0] - pa[0],
                    pc[1] - pa[1],
                    pc[2] - pa[2],
                )
                cx = uy * vz - uz * vy
                cy = uz * vx - ux * vz
                cz = ux * vy - uy * vx
                if (cx * cx + cy * cy + cz * cz) <= 1.0e-20:
                    stats["removed_total"] += 1
                    stats["removed_zero_area"] += 1
                    continue
            except Exception:
                pass
        filtered.extend(tri)

    return filtered, stats


def _sanitize_apx_triangle_array(
    array_elem,
    vertex_count: int | None = None,
    positions: List[Tuple[float, float, float]] | None = None,
) -> Dict[str, int]:
    indices = _parse_apx_int_array_text(getattr(array_elem, "text", ""))
    filtered, stats = _sanitize_apx_triangle_indices(
        indices,
        vertex_count,
        positions,
    )
    if stats["removed_total"]:
        array_elem.text = _format_apx_int_array_text(filtered)
    if str(array_elem.attrib.get("size", "")).strip() != str(len(filtered)):
        array_elem.attrib["size"] = str(len(filtered))
    stats["triangle_count"] = len(filtered) // 3
    return stats


def _clone_apx_xml_element(elem):
    return ElementTree.fromstring(ElementTree.tostring(elem, encoding="utf-8"))


def _parse_apx_float_array_text(text: str) -> List[float]:
    raw = str(text or "").replace(",", " ").split()
    return [float(value) for value in raw]


def _apx_destructible_submesh_positions(
    submesh,
    vertex_count: int,
) -> List[Tuple[float, float, float]] | None:
    try:
        vertex_format = _apx_find_child(
            submesh,
            "value",
            "name",
            "vertexFormat",
        )[0]
        buffer_formats = _apx_find_child(
            vertex_format,
            "array",
            "name",
            "bufferFormats",
        )
        buffers = _apx_find_child(submesh, "array", "name", "buffers")
        for idx, fmt_container in enumerate(buffer_formats):
            try:
                buffer_name = _apx_find_child(
                    fmt_container,
                    "value",
                    "name",
                    "name",
                ).text
            except Exception:
                continue
            if buffer_name != "SEMANTIC_POSITION" or idx >= len(buffers):
                continue
            data_elem = _apx_find_child(
                buffers[idx][0],
                "array",
                "name",
                "data",
            )
            values = _parse_apx_float_array_text(getattr(data_elem, "text", ""))
            usable_count = min(
                len(values) - (len(values) % 3),
                vertex_count * 3,
            )
            positions = [
                (values[i], values[i + 1], values[i + 2])
                for i in range(0, usable_count, 3)
            ]
            if len(positions) >= vertex_count:
                return positions[:vertex_count]
    except Exception:
        return None
    return None


def _sanitized_apx_cache_paths(path: str):
    try:
        stat = os.stat(path)
        cache_key = hashlib.sha1(
            f"{os.path.normcase(path)}|{stat.st_mtime_ns}|{stat.st_size}".encode(
                "utf-8"
            )
        ).hexdigest()[:12]
        out_dir = os.path.join(get_temp_root(create=True), "sanitized_apx")
        out_path = os.path.join(out_dir, f"{Path(path).stem}.{cache_key}.apx")
    except Exception:
        return None, None
    return out_path, out_path + ".clean"


def _write_sanitized_apx_copy(
    path: str,
    tree,
    change_notes: List[str],
) -> str:
    if not change_notes:
        return path
    try:
        out_path, _clean_marker = _sanitized_apx_cache_paths(path)
        if out_path is None:
            return path
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        tree.write(out_path, encoding="utf-8", xml_declaration=True)
        log.warning(
            "Using sanitized APX copy for %s: %s",
            os.path.basename(path),
            "; ".join(change_notes),
        )
        return out_path
    except Exception as exc:
        log.warning("Failed to write sanitized APX copy for %s: %s", path, exc)
        return path


def _sanitize_destructible_apx_for_import(path: str) -> str:
    try:
        tree = ElementTree.parse(path)
        root = tree.getroot()
        destructible = _apx_find_child(
            root,
            "value",
            "className",
            "DestructibleAssetParameters",
        )[0]
    except Exception as exc:
        log.debug(
            "Skipping destructible APX sanitization for %s: %s",
            path,
            exc,
        )
        return path

    change_notes: List[str] = []
    try:
        render_mesh = _apx_find_child(
            destructible,
            "value",
            "name",
            "renderMeshAsset",
        )[0]
        submeshes = _apx_find_child(
            render_mesh,
            "array",
            "name",
            "submeshes",
        )
    except Exception:
        return path

    for sub_idx, submesh_container in enumerate(submeshes):
        try:
            submesh = submesh_container[0][0][0]
            vertex_count_elem = _apx_find_child(
                submesh,
                "value",
                "name",
                "vertexCount",
            )
            vertex_count = int(str(vertex_count_elem.text or "0").strip())
            index_buffer_elem = _apx_find_child(
                submesh_container[0],
                "array",
                "name",
                "indexBuffer",
            )
        except Exception:
            continue
        positions = _apx_destructible_submesh_positions(submesh, vertex_count)
        stats = _sanitize_apx_triangle_array(
            index_buffer_elem,
            vertex_count,
            positions=positions,
        )
        if stats["removed_total"]:
            change_notes.append(
                f"destructible submesh {sub_idx}: "
                f"degenerate={stats['removed_degenerate']} "
                f"zero_area={stats['removed_zero_area']} "
                f"out_of_range={stats['removed_out_of_range']} "
                f"truncated={stats['removed_truncated']}"
            )

    return _write_sanitized_apx_copy(path, tree, change_notes)


def sanitize_apx_for_import(filepath: str) -> str:
    """Write a sanitized APX copy when triangle data would crash Blender import."""
    path = str(filepath or "").strip()
    if not path.lower().endswith(".apx") or not os.path.isfile(path):
        return filepath

    out_path, clean_marker = _sanitized_apx_cache_paths(path)
    if out_path is not None and os.path.isfile(out_path):
        return out_path
    if clean_marker is not None and os.path.isfile(clean_marker):
        return path
    result = _sanitize_apx_for_import_uncached(path)
    if clean_marker is not None and result == path:
        try:
            os.makedirs(os.path.dirname(clean_marker), exist_ok=True)
            open(clean_marker, "wb").close()
        except Exception:
            pass
    return result


def _sanitize_apx_for_import_uncached(path: str) -> str:
    try:
        tree = ElementTree.parse(path)
        root = tree.getroot()
        clothing = _apx_find_child(
            root,
            "value",
            "className",
            "ClothingAssetParameters",
        )[0]
    except Exception as exc:
        log.debug("Skipping clothing APX sanitization for %s: %s", path, exc)
        return _sanitize_destructible_apx_for_import(path)

    change_notes: List[str] = []

    graphical_lods = _apx_try_find_child(
        clothing,
        "array",
        "name",
        "graphicalLods",
    )
    physical_meshes = _apx_try_find_child(
        clothing,
        "array",
        "name",
        "physicalMeshes",
    )

    for array_elem, label in (
        (graphical_lods, "graphical lods"),
        (physical_meshes, "physical meshes"),
    ):
        if array_elem is None:
            continue
        original_count = len(array_elem)
        if original_count <= 1:
            continue
        for idx in range(original_count - 1, 0, -1):
            del array_elem[idx]
        array_elem.attrib["size"] = "1"
        change_notes.append(f"trimmed {label} {original_count}->1")

    required_sim_materials = max(
        len(graphical_lods) if graphical_lods is not None else 0,
        len(physical_meshes) if physical_meshes is not None else 0,
    )

    material_library = _apx_try_find_child(
        clothing,
        "value",
        "name",
        "materialLibrary",
    )
    if material_library is not None and len(material_library):
        materials_array = _apx_try_find_child(
            material_library[0],
            "array",
            "name",
            "materials",
        )
        if materials_array is not None:
            existing_sim_materials = len(materials_array)
            if (
                required_sim_materials
                and 0 < existing_sim_materials < required_sim_materials
            ):
                template_material = materials_array[-1]
                for _idx in range(
                    existing_sim_materials,
                    required_sim_materials,
                ):
                    materials_array.append(
                        _clone_apx_xml_element(template_material)
                    )
                materials_array.attrib["size"] = str(required_sim_materials)
                change_notes.append(
                    "duplicated simulation materials "
                    f"{existing_sim_materials}->{required_sim_materials}"
                )
            elif (
                existing_sim_materials
                and str(materials_array.attrib.get("size", "")).strip()
                != str(existing_sim_materials)
            ):
                materials_array.attrib["size"] = str(existing_sim_materials)
                change_notes.append(
                    "normalized simulation material array size to "
                    f"{existing_sim_materials}"
                )

    if graphical_lods is not None:
        for lod_idx, lod in enumerate(graphical_lods):
            try:
                render_mesh = _apx_find_child(
                    lod[0],
                    "value",
                    "name",
                    "renderMeshAsset",
                )[0]
                submeshes = _apx_find_child(
                    render_mesh,
                    "array",
                    "name",
                    "submeshes",
                )
            except Exception:
                continue
            for sub_idx, submesh_container in enumerate(submeshes):
                try:
                    submesh = submesh_container[0][0][0]
                    vertex_count_elem = _apx_find_child(
                        submesh,
                        "value",
                        "name",
                        "vertexCount",
                    )
                    vertex_count = int(
                        str(vertex_count_elem.text or "0").strip()
                    )
                    index_buffer_elem = _apx_find_child(
                        submesh_container[0],
                        "array",
                        "name",
                        "indexBuffer",
                    )
                except Exception:
                    continue
                stats = _sanitize_apx_triangle_array(
                    index_buffer_elem,
                    vertex_count,
                )
                if stats["removed_total"]:
                    change_notes.append(
                        f"graphical lod {lod_idx} submesh {sub_idx}: "
                        f"degenerate={stats['removed_degenerate']} "
                        f"zero_area={stats['removed_zero_area']} "
                        f"out_of_range={stats['removed_out_of_range']} "
                        f"truncated={stats['removed_truncated']}"
                    )

    if physical_meshes is not None:
        for phys_idx, physical_mesh_container in enumerate(physical_meshes):
            try:
                physical_mesh = physical_mesh_container[0][0]
                num_vertices_elem = _apx_find_child(
                    physical_mesh,
                    "value",
                    "name",
                    "numVertices",
                )
                num_indices_elem = _apx_find_child(
                    physical_mesh,
                    "value",
                    "name",
                    "numIndices",
                )
                indices_elem = _apx_find_child(
                    physical_mesh,
                    "array",
                    "name",
                    "indices",
                )
                vertex_count = int(
                    str(num_vertices_elem.text or "0").strip()
                )
            except Exception:
                continue
            stats = _sanitize_apx_triangle_array(indices_elem, vertex_count)
            if stats["removed_total"]:
                num_indices_elem.text = str(stats["triangle_count"] * 3)
                change_notes.append(
                    f"physical mesh {phys_idx}: "
                    f"degenerate={stats['removed_degenerate']} "
                    f"zero_area={stats['removed_zero_area']} "
                    f"out_of_range={stats['removed_out_of_range']} "
                    f"truncated={stats['removed_truncated']}"
                )

    if not change_notes:
        return path

    return _write_sanitized_apx_copy(path, tree, change_notes)
