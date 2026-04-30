import logging
from pathlib import Path
import inspect
import addon_utils
import bmesh
import json
import hashlib
import time

log = logging.getLogger(__name__)
_REDCLOTH_PROFILE_ENABLED = True
_REDCLOTH_PROFILE_WARN_THRESHOLD = 0.10


def _log_redcloth_profile_warning(message, *args):
    if not _REDCLOTH_PROFILE_ENABLED:
        return
    log.info("[redcloth-profile] " + str(message), *args)

from .importers.import_rig import rotate_and_connect_bones
from .w3_material import create_param, read_2wmi_params2, setup_w3_material, xml_data_from_CR2W
from . import CR2W, get_do_fix_tail
import bpy, os, filecmp, shutil
from typing import List, Tuple, Dict
from bpy.types import Image, Material, Object, Node
import re
import numpy as np
from xml.etree import ElementTree
Element = ElementTree.Element
from xml.dom import minidom

from . import get_uncook_path
from . import get_fbx_uncook_path
from . import get_texture_path
from . import get_DO_WEAR_CLOTH, get_redcloth_simulation_enabled, get_redcloth_wind_velocity
from .extension_paths import get_temp_root

from . import CR2W
import bpy

log = logging.getLogger(__name__)

def _resolve_collection_ref(collection_ref):
    if collection_ref is None:
        return None
    if hasattr(collection_ref, "collection") and getattr(collection_ref, "collection", None) is not None:
        return collection_ref.collection
    if hasattr(collection_ref, "objects") and hasattr(collection_ref, "children"):
        return collection_ref
    if collection_ref == "Scene Collection":
        return bpy.context.scene.collection
    return bpy.data.collections.get(str(collection_ref))


def _find_layer_collection_for_collection(layer_collection, target_collection):
    if layer_collection is None or target_collection is None:
        return None
    if getattr(layer_collection, "collection", None) == target_collection:
        return layer_collection
    for child in getattr(layer_collection, "children", []):
        found = _find_layer_collection_for_collection(child, target_collection)
        if found is not None:
            return found
    return None


def _restore_active_layer_collection_for_collection(context, target_collection):
    if target_collection is None:
        return False
    ctx = context or bpy.context
    view_layer = getattr(ctx, "view_layer", None)
    if view_layer is None:
        return False
    target_layer = _find_layer_collection_for_collection(
        getattr(view_layer, "layer_collection", None),
        target_collection,
    )
    if target_layer is None:
        return False
    view_layer.active_layer_collection = target_layer
    return True


def move_objects_between_collections(old_collection_name, new_collection_name):
    # Get the master collection (Scene Collection)
    master_collection = bpy.context.scene.collection

    old_collection = _resolve_collection_ref(old_collection_name)
    if old_collection is None:
        log.warning("Old collection '%s' not found.", old_collection_name)
        return

    new_collection = _resolve_collection_ref(new_collection_name)
    if new_collection is None:
        new_collection_label = str(new_collection_name or "").strip()
        if not new_collection_label:
            log.warning("New collection '%s' not found.", new_collection_name)
            return
        log.debug("New collection '%s' not found. Creating it.", new_collection_label)
        new_collection = bpy.data.collections.new(new_collection_label)
        master_collection.children.link(new_collection)

    if old_collection == new_collection:
        return

    old_collection_name = getattr(old_collection, "name", str(old_collection_name))
    new_collection_name = getattr(new_collection, "name", str(new_collection_name))

    # Move all objects from old collection to new collection
    objects_to_move = list(old_collection.objects)
    for obj in objects_to_move:
        # Link object to new collection if not already linked
        if obj.name not in new_collection.objects:
            new_collection.objects.link(obj)
        # Unlink object from old collection if it's not the Scene Collection
        if old_collection != master_collection:
            old_collection.objects.unlink(obj)

    # Move all child collections from old collection to new collection
    child_collections = list(old_collection.children)
    for child in child_collections:
        if child.name not in new_collection.children.keys():
            new_collection.children.link(child)
        if old_collection != master_collection:
            old_collection.children.unlink(child)

    # Attempt to delete old collection if it's not the Scene Collection
    if old_collection != master_collection:
        # Check if old collection is empty
        if not old_collection.objects and not old_collection.children:
            # Unlink old collection from any parent collections
            parents = [coll for coll in bpy.data.collections if old_collection.name in coll.children.keys()]
            for parent in parents:
                parent.children.unlink(old_collection)
            # Remove old collection from bpy.data.collections
            bpy.data.collections.remove(old_collection)
            log.debug("Old collection '%s' deleted.", old_collection_name)
        else:
            log.debug("Old collection '%s' is not empty, keeping.", old_collection_name)
    else:
        log.debug("Cannot delete 'Scene Collection'.")


def prettify(elem):
    """Return a pretty-printed XML string for the Element.
    """
    rough_string = ElementTree.tostring(elem, 'utf-8')
    try:
        reparsed = minidom.parseString(rough_string)
        return reparsed.toprettyxml(indent="\t")
    except Exception:
        log.warning("Material XML prettify failed; storing compact XML instead.", exc_info=True)
        return rough_string.decode("utf-8", errors="ignore")

def setup_w3_material_CR2W(
        uncook_path: str
        ,bl_material: Material
        ,mat_bin:str
        ,force_update = False	# Set to True when re-importing stuff to test changes with the latest material set-up code.
        ,mat_filename = str
        ,is_instance_file = False
        ):
        new_xml = xml_data_from_CR2W(mat_bin, bl_material.name)
        bl_material.use_nodes = True
                    
        ##return base mat path and if it is local chunk handle
        bl_material.witcher_props.name = bl_material.name
        #bl_material.witcher_props.base = "custom"
        bl_material.witcher_props.base_custom = new_xml.get('base')
        bl_material.witcher_props.local = True
        bl_material.witcher_props.xml_text = prettify(new_xml)
        #enableMask
        # if hasattr(mat_bin , 'local') and mat_bin.local == True:
        #     bl_material.witcher_props.local = True
        if hasattr(mat_bin ,'DepotPath') and hasattr(mat_bin , 'local') and mat_bin.local == False:
            bl_material.witcher_props.base_custom = mat_bin.DepotPath
            bl_material.witcher_props.local = False
        
        if mat_bin.get_CR2W_version() <= 115:
            bl_material.witcher_props.material_version = "witcher2"
            
        enableMask = mat_bin.GetVariableByName('enableMask')
        if enableMask and enableMask.Value == 1:
            bl_material.witcher_props.enableMask = True
        return setup_w3_material(uncook_path, bl_material, xml_data=new_xml, xml_path=mat_filename, force_update=force_update, is_instance_file = is_instance_file)

def load_w3_materials_CR2W(
        obj: Object
        ,uncook_path: str
        ,materials_bin: str
        ,material_names: str
        ,force_mat_update = False
        ,mat_filename = str
    ):
    for idx, mat in enumerate(materials_bin):
        if mat is None:
            log.warning(f"Skipping unresolved material at slot {idx} ({material_names[idx] if idx < len(material_names) else '?'})")
            continue
        xml_mat_name = material_names[idx]
        log.info(xml_mat_name)
        target_mat = _find_matching_material_on_object(obj, xml_mat_name)
        if not target_mat:
            # Didn't find a matching blender material.
            # Must be a material that's only for LODs, so let's ignore.
            continue

        finished_mat = setup_w3_material_CR2W(uncook_path, target_mat, mat, force_update=force_mat_update, mat_filename=mat_filename)
        obj.material_slots[target_mat.name].material = finished_mat


def _material_name_base(name: str) -> str:
    return re.sub(r"\.\d{3}$", "", str(name or ""))


def _find_matching_material_on_object(obj: Object, xml_mat_name: str):
    if obj is None or getattr(obj, "type", None) != 'MESH':
        return None

    try:
        if xml_mat_name in obj.data.materials:
            return obj.data.materials[xml_mat_name]
    except Exception:
        pass

    xml_base = _material_name_base(xml_mat_name)
    for m in obj.data.materials:
        if m is None:
            continue
        m_base = _material_name_base(m.name)
        if m_base == xml_base:
            log.info("redcloth material base match %s -> %s", xml_mat_name, m.name)
            return m
        if xml_base and (xml_base in m_base or m_base in xml_base):
            log.info("redcloth material partial match %s -> %s", xml_mat_name, m.name)
            return m
    return None


def _redcloth_material_name_prefix(redcloth_resource: str = "", fallback_path: str = "") -> str:
    source_path = str(redcloth_resource or fallback_path or "").strip()
    if not source_path:
        return ""
    return Path(source_path.replace("/", "\\")).stem


def _read_redcloth_material_payload(redcloth_resource: str, mat_filename: str):
    started = time.perf_counter()
    redcloth_material = None
    materials = []
    material_names = []
    mat_filename = str(mat_filename or "").strip()
    if mat_filename:
        redcloth_material = CR2W.CR2W_reader.load_material(mat_filename)
    prefix = _redcloth_material_name_prefix(redcloth_resource, mat_filename)
    if redcloth_material:
        for chunk in redcloth_material:
            if chunk.name not in {"CApexClothResource", "CApexDestructionResource"}:
                continue
            materials_handle = chunk.GetVariableByName('materials')
            if materials_handle and hasattr(materials_handle, "Handles"):
                materials = [redcloth_material[o.Reference] for o in materials_handle.Handles]
            apex_names = chunk.GetVariableByName('apexMaterialNames')
            if apex_names and hasattr(apex_names, "elements"):
                material_names = []
                for element in apex_names.elements:
                    raw_name = str(getattr(element, "String", "") or "")
                    suffix = raw_name.split("::", 1)[1] if "::" in raw_name else raw_name
                    material_names.append(prefix + suffix)
            break
    return redcloth_material, materials, material_names, time.perf_counter() - started


def apply_redcloth_materials_to_meshes(
    mesh_objects,
    redcloth_resource: str,
    mat_filename: str,
    *,
    context=None,
    force_mat_update: bool = False,
    apply_runtime_defaults: bool = False,
):
    mesh_list = [obj for obj in (mesh_objects or []) if obj is not None and getattr(obj, "type", None) == 'MESH']
    result = {
        "read_seconds": 0.0,
        "apply_seconds": 0.0,
        "material_count": 0,
        "mesh_count": len(mesh_list),
    }
    mat_filename = str(mat_filename or "").strip()
    if not mesh_list or not mat_filename:
        return result

    ctx = context or bpy.context
    uncook_path = get_texture_path(ctx) + "\\"
    redcloth_material, materials, material_names, read_seconds = _read_redcloth_material_payload(redcloth_resource, mat_filename)
    result["read_seconds"] = read_seconds
    result["material_count"] = len(material_names)

    if not redcloth_material or not materials or not material_names:
        if apply_runtime_defaults:
            for mesh_obj in mesh_list:
                _apply_redcloth_runtime_defaults(mesh_obj, ctx)
        return result

    total_apply_seconds = 0.0
    for mesh_obj in mesh_list:
        target_mat = False
        for idx, _mat in enumerate(materials):
            if idx >= len(material_names):
                break
            xml_mat_name = material_names[idx]
            target_mat = _find_matching_material_on_object(mesh_obj, xml_mat_name)
            if target_mat:
                break

        if not target_mat and material_names:
            for idx, material in enumerate(mesh_obj.data.materials):
                if idx >= len(material_names):
                    break
                if material is not None:
                    material.name = material_names[idx]

        apply_started = time.perf_counter()
        load_w3_materials_CR2W(
            mesh_obj,
            uncook_path,
            materials,
            material_names,
            force_mat_update=force_mat_update,
            mat_filename=mat_filename,
        )
        total_apply_seconds += time.perf_counter() - apply_started

        if apply_runtime_defaults:
            _apply_redcloth_runtime_defaults(mesh_obj, ctx)

    result["apply_seconds"] = total_apply_seconds
    return result


def getGeometryCenter(obj):
		sumWCoord = [0,0,0]
		numbVert = 0
		if obj.type == 'MESH':
			for vert in obj.data.vertices:
				wmtx = obj.matrix_world
				worldCoord = vert.co @ wmtx
				sumWCoord[0] += worldCoord[0]
				sumWCoord[1] += worldCoord[1]
				sumWCoord[2] += worldCoord[2]
				numbVert += 1
			sumWCoord[0] = sumWCoord[0]/numbVert
			sumWCoord[1] = sumWCoord[1]/numbVert
			sumWCoord[2] = sumWCoord[2]/numbVert
		return sumWCoord
	
def setOrigin(obj):
    oldLoc = obj.location
    newLoc = getGeometryCenter(obj)
    for vert in obj.data.vertices:
        vert.co[0] -= newLoc[0] - oldLoc[0]
        vert.co[1] -= newLoc[1] - oldLoc[1]
        vert.co[2] -= newLoc[2] - oldLoc[2]
    obj.location = newLoc 

def createEmpty(prefix = None, name = "", parent = None):
    bpy.ops.object.empty_add(type="PLAIN_AXES", radius=0.1)
    transform = bpy.context.object
    transform.name = prefix+":"+name if prefix else name
    transform.parent = parent if parent else None
    return transform


def _namespaced_name(prefix: str, name: str) -> str:
    if not prefix:
        return name
    prefix_tag = f"{prefix}:"
    if name.startswith(prefix_tag):
        return name
    return prefix_tag + name


def _find_clothsimulation_modifier(obj: Object):
    if obj is None or obj.type != 'MESH':
        return None
    for mod in obj.modifiers:
        if mod.type != 'NODES':
            continue
        if mod.name == "ClothSimulation":
            return mod
        node_group = getattr(mod, "node_group", None)
        if node_group and node_group.name.startswith("ClothSimulation"):
            return mod
    return None


def _apply_redcloth_runtime_defaults(cloth_obj: Object, context) -> None:
    """Apply global runtime defaults (simulation enabled + wind velocity) to imported APX cloth."""
    mod = _find_clothsimulation_modifier(cloth_obj)
    if mod is None:
        return

    try:
        sim_enabled = bool(get_redcloth_simulation_enabled(context))
    except Exception:
        sim_enabled = True
    try:
        wind_velocity = float(get_redcloth_wind_velocity(context))
    except Exception:
        wind_velocity = 0.0

    try:
        mod.show_viewport = sim_enabled
    except Exception:
        pass
    try:
        mod.show_render = sim_enabled
    except Exception:
        pass

    try:
        mod["Socket_5"] = wind_velocity
        node_group = getattr(mod, "node_group", None)
        if node_group is not None:
            node_group.interface_update(context)
    except Exception as e:
        log.debug("Could not apply redcloth wind velocity to %s: %s", getattr(cloth_obj, "name", "<cloth>"), e)

    # Do not write into io_mesh_apx UI state here. Its update callback assumes the
    # active object owns a ClothSimulation modifier and can raise during batch/entity import.


def _merge_mesh_by_distance_data(mesh_obj: Object, merge_threshold: float = 0.0001) -> None:
    """Context-safe vertex merge used by redcloth import (avoids edit-mode operator poll failures)."""
    if mesh_obj is None or mesh_obj.type != 'MESH':
        return
    mesh = getattr(mesh_obj, "data", None)
    if mesh is None or len(getattr(mesh, "vertices", [])) == 0:
        return

    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        if bm.verts:
            bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=merge_threshold)
            bm.to_mesh(mesh)
            mesh.update()
    finally:
        bm.free()


def _apx_find_child(root, tag: str, attr: str | None = None, attr_value: str | None = None):
    for elem in root:
        if elem.tag != tag:
            continue
        if attr is None or elem.attrib.get(attr) == attr_value:
            return elem
    raise LookupError(f"Missing APX element {tag} {attr}={attr_value}")


def _apx_try_find_child(root, tag: str, attr: str | None = None, attr_value: str | None = None):
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
                ux, uy, uz = (pb[0] - pa[0], pb[1] - pa[1], pb[2] - pa[2])
                vx, vy, vz = (pc[0] - pa[0], pc[1] - pa[1], pc[2] - pa[2])
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
    filtered, stats = _sanitize_apx_triangle_indices(indices, vertex_count, positions)
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


def _apx_destructible_submesh_positions(submesh, vertex_count: int) -> List[Tuple[float, float, float]] | None:
    try:
        vertex_format = _apx_find_child(submesh, "value", "name", "vertexFormat")[0]
        buffer_formats = _apx_find_child(vertex_format, "array", "name", "bufferFormats")
        buffers = _apx_find_child(submesh, "array", "name", "buffers")
        for idx, fmt_container in enumerate(buffer_formats):
            try:
                buffer_name = _apx_find_child(fmt_container, "value", "name", "name").text
            except Exception:
                continue
            if buffer_name != "SEMANTIC_POSITION" or idx >= len(buffers):
                continue
            data_elem = _apx_find_child(buffers[idx][0], "array", "name", "data")
            values = _parse_apx_float_array_text(getattr(data_elem, "text", ""))
            usable_count = min(len(values) - (len(values) % 3), vertex_count * 3)
            positions = [
                (values[i], values[i + 1], values[i + 2])
                for i in range(0, usable_count, 3)
            ]
            if len(positions) >= vertex_count:
                return positions[:vertex_count]
    except Exception:
        return None
    return None


def _write_sanitized_apx_copy(path: str, tree, change_notes: List[str]) -> str:
    if not change_notes:
        return path
    try:
        stat = os.stat(path)
        cache_key = hashlib.sha1(
            f"{os.path.normcase(path)}|{stat.st_mtime_ns}|{stat.st_size}".encode("utf-8")
        ).hexdigest()[:12]
        out_dir = os.path.join(get_temp_root(create=True), "sanitized_apx")
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, f"{Path(path).stem}.{cache_key}.apx")
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
        destructible = _apx_find_child(root, "value", "className", "DestructibleAssetParameters")[0]
    except Exception as exc:
        log.debug("Skipping destructible APX sanitization for %s: %s", path, exc)
        return path

    change_notes: List[str] = []
    try:
        render_mesh = _apx_find_child(destructible, "value", "name", "renderMeshAsset")[0]
        submeshes = _apx_find_child(render_mesh, "array", "name", "submeshes")
    except Exception:
        return path

    for sub_idx, submesh_container in enumerate(submeshes):
        try:
            submesh = submesh_container[0][0][0]
            vertex_count_elem = _apx_find_child(submesh, "value", "name", "vertexCount")
            vertex_count = int(str(vertex_count_elem.text or "0").strip())
            index_buffer_elem = _apx_find_child(submesh_container[0], "array", "name", "indexBuffer")
        except Exception:
            continue
        positions = _apx_destructible_submesh_positions(submesh, vertex_count)
        stats = _sanitize_apx_triangle_array(index_buffer_elem, vertex_count, positions=positions)
        if stats["removed_total"]:
            change_notes.append(
                f"destructible submesh {sub_idx}: "
                f"degenerate={stats['removed_degenerate']} "
                f"zero_area={stats['removed_zero_area']} "
                f"out_of_range={stats['removed_out_of_range']} "
                f"truncated={stats['removed_truncated']}"
            )

    return _write_sanitized_apx_copy(path, tree, change_notes)


def _sanitize_apx_for_import(filepath: str) -> str:
    """Write a sanitized APX copy when triangle data would crash Blender import."""
    path = str(filepath or "").strip()
    if not path.lower().endswith(".apx") or not os.path.isfile(path):
        return filepath

    try:
        tree = ElementTree.parse(path)
        root = tree.getroot()
        clothing = _apx_find_child(root, "value", "className", "ClothingAssetParameters")[0]
    except Exception as exc:
        log.debug("Skipping clothing APX sanitization for %s: %s", path, exc)
        return _sanitize_destructible_apx_for_import(path)

    change_notes: List[str] = []

    graphical_lods = _apx_try_find_child(clothing, "array", "name", "graphicalLods")
    physical_meshes = _apx_try_find_child(clothing, "array", "name", "physicalMeshes")

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

    material_library = _apx_try_find_child(clothing, "value", "name", "materialLibrary")
    if material_library is not None and len(material_library):
        materials_array = _apx_try_find_child(material_library[0], "array", "name", "materials")
        if materials_array is not None:
            existing_sim_materials = len(materials_array)
            if required_sim_materials and 0 < existing_sim_materials < required_sim_materials:
                template_material = materials_array[-1]
                for _idx in range(existing_sim_materials, required_sim_materials):
                    materials_array.append(_clone_apx_xml_element(template_material))
                materials_array.attrib["size"] = str(required_sim_materials)
                change_notes.append(
                    f"duplicated simulation materials {existing_sim_materials}->{required_sim_materials}"
                )
            elif existing_sim_materials and str(materials_array.attrib.get("size", "")).strip() != str(existing_sim_materials):
                materials_array.attrib["size"] = str(existing_sim_materials)
                change_notes.append(
                    f"normalized simulation material array size to {existing_sim_materials}"
                )

    if graphical_lods is not None:
        for lod_idx, lod in enumerate(graphical_lods):
            try:
                render_mesh = _apx_find_child(lod[0], "value", "name", "renderMeshAsset")[0]
                submeshes = _apx_find_child(render_mesh, "array", "name", "submeshes")
            except Exception:
                continue
            for sub_idx, submesh_container in enumerate(submeshes):
                try:
                    submesh = submesh_container[0][0][0]
                    vertex_count_elem = _apx_find_child(submesh, "value", "name", "vertexCount")
                    vertex_count = int(str(vertex_count_elem.text or "0").strip())
                    index_buffer_elem = _apx_find_child(submesh_container[0], "array", "name", "indexBuffer")
                except Exception:
                    continue
                stats = _sanitize_apx_triangle_array(index_buffer_elem, vertex_count)
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
                num_vertices_elem = _apx_find_child(physical_mesh, "value", "name", "numVertices")
                num_indices_elem = _apx_find_child(physical_mesh, "value", "name", "numIndices")
                indices_elem = _apx_find_child(physical_mesh, "array", "name", "indices")
                vertex_count = int(str(num_vertices_elem.text or "0").strip())
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
        return filepath

    return _write_sanitized_apx_copy(path, tree, change_notes)


def _bpy_data_block_identity(data_block):
    if data_block is None:
        return None
    try:
        return int(data_block.as_pointer())
    except Exception:
        return id(data_block)


def _snapshot_blender_import_state():
    return {
        "objects": {_bpy_data_block_identity(obj) for obj in bpy.data.objects},
        "collections": {_bpy_data_block_identity(coll) for coll in bpy.data.collections},
        "meshes": {_bpy_data_block_identity(mesh) for mesh in bpy.data.meshes},
        "armatures": {_bpy_data_block_identity(arm) for arm in bpy.data.armatures},
        "materials": {_bpy_data_block_identity(mat) for mat in bpy.data.materials},
        "node_groups": {_bpy_data_block_identity(group) for group in bpy.data.node_groups},
    }


def _object_parent_depth(obj):
    depth = 0
    current = getattr(obj, "parent", None)
    while current is not None:
        depth += 1
        current = getattr(current, "parent", None)
    return depth


def _iter_collection_parents(collection):
    if collection is None:
        return
    scene = getattr(bpy.context, "scene", None)
    scene_collection = getattr(scene, "collection", None)
    if scene_collection is not None:
        try:
            if collection.name in scene_collection.children.keys():
                yield scene_collection
        except Exception:
            pass
    for parent in bpy.data.collections:
        try:
            if collection.name in parent.children.keys():
                yield parent
        except Exception:
            continue


def _collection_parent_depth(collection, _memo=None):
    if collection is None:
        return 0
    if _memo is None:
        _memo = {}
    coll_id = _bpy_data_block_identity(collection)
    if coll_id in _memo:
        return _memo[coll_id]
    parents = list(_iter_collection_parents(collection))
    if not parents:
        _memo[coll_id] = 0
        return 0
    depth = 1 + max(_collection_parent_depth(parent, _memo) for parent in parents)
    _memo[coll_id] = depth
    return depth


def _cleanup_failed_cloth_import(snapshot_state):
    if not snapshot_state:
        return

    new_objects = [
        obj for obj in bpy.data.objects
        if _bpy_data_block_identity(obj) not in snapshot_state["objects"]
    ]
    new_objects.sort(key=_object_parent_depth, reverse=True)
    for obj in new_objects:
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
        except Exception as exc:
            log.debug("Failed removing partial cloth import object %s: %s", getattr(obj, "name", "<unknown>"), exc)

    depth_cache = {}
    new_collections = [
        coll for coll in bpy.data.collections
        if _bpy_data_block_identity(coll) not in snapshot_state["collections"]
    ]
    new_collections.sort(key=lambda coll: _collection_parent_depth(coll, depth_cache), reverse=True)
    for coll in new_collections:
        try:
            for parent in list(_iter_collection_parents(coll)):
                try:
                    parent.children.unlink(coll)
                except Exception:
                    pass
            bpy.data.collections.remove(coll)
        except Exception as exc:
            log.debug("Failed removing partial cloth import collection %s: %s", getattr(coll, "name", "<unknown>"), exc)

    orphan_specs = [
        (bpy.data.node_groups, "node_groups"),
        (bpy.data.materials, "materials"),
        (bpy.data.meshes, "meshes"),
        (bpy.data.armatures, "armatures"),
    ]
    for datablocks, snapshot_key in orphan_specs:
        for datablock in list(datablocks):
            if _bpy_data_block_identity(datablock) in snapshot_state[snapshot_key]:
                continue
            try:
                if getattr(datablock, "users", 0) == 0:
                    datablocks.remove(datablock)
            except Exception as exc:
                log.debug(
                    "Failed removing partial cloth import data block %s: %s",
                    getattr(datablock, "name", "<unknown>"),
                    exc,
                )


def _fix_connection_objects_transform_space(connection_objects):
    """Switch Object Info nodes in SphereConnection GN groups from ORIGINAL to RELATIVE.

    APX creates connection objects whose SphereConnectionTemplate GN reads sphere
    positions in ORIGINAL (world) space.  When these objects are parented to a moving
    hierarchy the parent chain shifts them by D AND the GN geometry is also already at
    the new world position → double transform.  RELATIVE makes the geometry relative to
    the modifier object, so the parent chain offset cancels correctly.
    """
    for obj in connection_objects:
        if obj is None or obj.name not in bpy.data.objects:
            continue
        for mod in obj.modifiers:
            ng = getattr(mod, 'node_group', None)
            if ng is None:
                continue
            for node in ng.nodes:
                if node.bl_idname == 'GeometryNodeObjectInfo' and hasattr(node, 'transform_space'):
                    try:
                        node.transform_space = 'RELATIVE'
                    except Exception as e:
                        log.debug("Could not set transform_space RELATIVE on %s in %s: %s", node.name, ng.name, e)


def _parent_and_namespace_collision_objects(prefix: str, parent_obj: Object, objects, keep_transform: bool = False):
    if parent_obj is None or not objects:
        return
    bpy.context.view_layer.objects.active = None
    bpy.ops.object.select_all(action='DESELECT')
    parent_obj.select_set(True)
    bpy.context.view_layer.objects.active = parent_obj
    selected_count = 0
    for obj in objects:
        if obj is None or obj.name not in bpy.data.objects:
            continue
        obj.name = _namespaced_name(prefix, obj.name)
        obj.select_set(True)
        selected_count += 1
    if selected_count:
        bpy.ops.object.parent_set(type='OBJECT', keep_transform=keep_transform)


def _link_objects_to_collection(collection, objects):
    if collection is None:
        return
    for obj in objects:
        if obj is None or obj.name not in bpy.data.objects:
            continue
        if collection not in obj.users_collection:
            collection.objects.link(obj)


def _unlink_objects_from_collection(collection, objects):
    if collection is None:
        return
    for obj in objects:
        if obj is None or obj.name not in bpy.data.objects:
            continue
        if collection in obj.users_collection:
            try:
                collection.objects.unlink(obj)
            except Exception:
                pass


def _remove_collection_if_exists(collection):
    if collection is None:
        return
    try:
        bpy.data.collections.remove(collection)
    except Exception as e:
        log.debug("Could not remove collection %s: %s", getattr(collection, "name", "<unknown>"), e)


def _ensure_geometry_output_interface(node_group):
    """Create a geometry output socket for runtime-created GN groups (Blender 4.x API)."""
    try:
        node_group.interface.new_socket(
            name="Geometry",
            in_out='OUTPUT',
            socket_type='NodeSocketGeometry',
        )
        return
    except Exception:
        pass
    # Blender compatibility fallback (older API)
    try:
        node_group.outputs.new("NodeSocketGeometry", "Geometry")
    except Exception:
        pass


def _find_socket_by_names(sockets, names):
    names_l = {n.lower() for n in names if n}
    for sock in sockets:
        if (getattr(sock, "name", "") or "").lower() in names_l:
            return sock
    return None


def _first_geometry_output(node):
    for sock in getattr(node, "outputs", []):
        if getattr(sock, "type", None) == 'GEOMETRY':
            return sock
    if getattr(node, "outputs", None):
        return node.outputs[0]
    return None


def _first_geometry_input(node):
    for sock in getattr(node, "inputs", []):
        if getattr(sock, "type", None) == 'GEOMETRY':
            return sock
    if getattr(node, "inputs", None):
        return node.inputs[0]
    return None


def _role_from_token(token: str):
    t = (token or "").lower()
    if not t:
        return None
    if "input_14" in t or "collision sphere" in t or "spheres" in t or "sphere" in t:
        return "spheres"
    if "socket_2" in t or "connection" in t:
        return "connections"
    if "socket_3" in t or "capsule" in t:
        return "capsules"
    return None


def _classify_collection_info_node(node, mod=None):
    # Try linked group-input socket identifiers first (usually Input_14/Socket_2/Socket_3).
    coll_input = _find_socket_by_names(node.inputs, {"Collection"})
    if coll_input and coll_input.is_linked:
        link = coll_input.links[0]
        from_sock = link.from_socket
        for token in (
            getattr(from_sock, "identifier", ""),
            getattr(from_sock, "name", ""),
        ):
            role = _role_from_token(token)
            if role:
                return role

        if mod is not None:
            for token in (getattr(from_sock, "identifier", ""), getattr(from_sock, "name", "")):
                if not token:
                    continue
                try:
                    coll_val = mod[token]
                except Exception:
                    coll_val = None
                role = _role_from_token(getattr(coll_val, "name", ""))
                if role:
                    return role

    # Fall back to node label/name/default collection name.
    for token in (
        getattr(node, "label", ""),
        getattr(node, "name", ""),
    ):
        role = _role_from_token(token)
        if role:
            return role
    try:
        default_coll = coll_input.default_value if coll_input else None
        role = _role_from_token(getattr(default_coll, "name", ""))
        if role:
            return role
    except Exception:
        pass
    return None


def _create_object_join_proxy_nodegroup(group_name: str, source_objects):
    ng = bpy.data.node_groups.new(group_name, 'GeometryNodeTree')
    _ensure_geometry_output_interface(ng)

    nodes = ng.nodes
    links = ng.links
    nodes.clear()

    group_out = nodes.new('NodeGroupOutput')
    group_out.location = (450, 0)
    join = nodes.new('GeometryNodeJoinGeometry')
    join.location = (220, 0)

    y = 0
    for src_obj in source_objects:
        if src_obj is None or src_obj.name not in bpy.data.objects:
            continue
        obj_info = nodes.new('GeometryNodeObjectInfo')
        obj_info.label = f"ProxySource: {src_obj.name}"
        obj_info.location = (-120, y)

        obj_socket = _find_socket_by_names(obj_info.inputs, {"Object"})
        if obj_socket is not None:
            try:
                obj_socket.default_value = src_obj
            except Exception as e:
                log.debug("Failed assigning proxy source object %s: %s", src_obj.name, e)

        as_instance_socket = _find_socket_by_names(obj_info.inputs, {"As Instance"})
        if as_instance_socket is not None:
            try:
                as_instance_socket.default_value = False
            except Exception:
                pass

        if hasattr(obj_info, "transform_space"):
            try:
                obj_info.transform_space = 'RELATIVE'
            except Exception:
                pass

        out_sock = _first_geometry_output(obj_info)
        in_sock = _first_geometry_input(join)
        if out_sock is not None and in_sock is not None:
            links.new(out_sock, in_sock)
        y -= 140

    join_out = _first_geometry_output(join)
    out_in = _first_geometry_input(group_out)
    if join_out is not None and out_in is not None:
        links.new(join_out, out_in)

    return ng


def _create_collision_proxy_object(name: str, parent: Object, owner_collection, source_objects):
    if not source_objects:
        return None

    mesh = bpy.data.meshes.new(name + "_MESH")
    proxy = bpy.data.objects.new(name, mesh)
    owner_collection.objects.link(proxy)
    proxy.parent = parent
    proxy.hide_render = True
    proxy.hide_select = True
    try:
        proxy.display_type = 'WIRE'
    except Exception:
        pass
    try:
        proxy["witcher_apx_collision_proxy"] = True
    except Exception:
        pass

    node_group = _create_object_join_proxy_nodegroup(name + "_GN", source_objects)
    mod = proxy.modifiers.new(name="WitcherAPXCollisionProxy", type='NODES')
    mod.node_group = node_group
    return proxy


def _replace_collection_info_node_with_object_info(node_group, coll_node, proxy_obj):
    if node_group is None or coll_node is None or proxy_obj is None:
        return False

    nodes = node_group.nodes
    links = node_group.links

    obj_info = nodes.new('GeometryNodeObjectInfo')
    obj_info.location = coll_node.location
    obj_info.label = f"Proxy:{proxy_obj.name}"
    obj_info.name = coll_node.name + "_Proxy"

    obj_socket = _find_socket_by_names(obj_info.inputs, {"Object"})
    if obj_socket is not None:
        obj_socket.default_value = proxy_obj
    as_instance_socket = _find_socket_by_names(obj_info.inputs, {"As Instance"})
    if as_instance_socket is not None:
        try:
            as_instance_socket.default_value = False
        except Exception:
            pass
    if hasattr(obj_info, "transform_space"):
        try:
            obj_info.transform_space = 'RELATIVE'
        except Exception:
            pass

    old_out = _first_geometry_output(coll_node)
    new_out = _first_geometry_output(obj_info)
    if old_out is None or new_out is None:
        nodes.remove(obj_info)
        return False

    outgoing = [(link.to_socket, link.to_node) for link in list(old_out.links)]
    for to_socket, _to_node in outgoing:
        try:
            links.new(new_out, to_socket)
        except Exception as e:
            log.debug("Failed rewiring cloth collision proxy link on %s: %s", node_group.name, e)

    try:
        nodes.remove(coll_node)
    except Exception:
        return False
    return True


def _find_clothsim_colliders_group_node(node_group):
    if node_group is None:
        return None
    # APX template (from your example script) usually uses "Group.008" for SoftBody.Init.Colliders.
    for node in node_group.nodes:
        if node.bl_idname != 'GeometryNodeGroup':
            continue
        sub_tree = getattr(node, "node_tree", None)
        sub_name = getattr(sub_tree, "name", "") or ""
        node_name = getattr(node, "name", "") or ""
        if "SoftBody.Init.Colliders" in sub_name or "Softbody.Init Colliders" in sub_name:
            return node
        if node_name == "Group.008":
            return node
    return None


def _patch_clothsim_groupnode_colliders_to_proxies(node_group, proxy_objects: Dict[str, Object]) -> bool:
    """Patch the actual APX ClothSimulation group node graph (Group.008) to use object proxies."""
    colliders_group_node = _find_clothsim_colliders_group_node(node_group)
    if colliders_group_node is None:
        return False

    nodes = node_group.nodes
    links = node_group.links
    colliders_out = _first_geometry_output(colliders_group_node)
    if colliders_out is None:
        return False

    # Collect outgoing targets before rewiring.
    outgoing_links = list(colliders_out.links)
    outgoing_targets = [lnk.to_socket for lnk in outgoing_links]

    # Create or reuse a join node inside the actual ClothSimulation node tree.
    join_name = "WitcherAPXColliderProxyJoin"
    join_node = nodes.get(join_name)
    if join_node is None or join_node.bl_idname != 'GeometryNodeJoinGeometry':
        if join_node is not None:
            nodes.remove(join_node)
        join_node = nodes.new('GeometryNodeJoinGeometry')
        join_node.name = join_name
    join_node.label = "Witcher APX Collision Proxies"
    try:
        base_loc = colliders_group_node.location
        join_node.location = (base_loc[0] + 240.0, base_loc[1] - 40.0)
    except Exception:
        pass

    join_input = _first_geometry_input(join_node)
    join_output = _first_geometry_output(join_node)
    if join_input is None or join_output is None:
        return False

    # Reset join inputs so reruns don't duplicate links.
    for link in list(join_input.links):
        try:
            links.remove(link)
        except Exception:
            pass

    role_order = ("spheres", "connections", "capsules")
    role_y = {
        "spheres": 160.0,
        "connections": 0.0,
        "capsules": -160.0,
    }
    wired_any_proxy = False
    for role in role_order:
        proxy_obj = proxy_objects.get(role)
        if proxy_obj is None or proxy_obj.name not in bpy.data.objects:
            continue

        node_name = f"WitcherAPXColliderProxy_{role}"
        obj_info = nodes.get(node_name)
        if obj_info is None or obj_info.bl_idname != 'GeometryNodeObjectInfo':
            if obj_info is not None:
                nodes.remove(obj_info)
            obj_info = nodes.new('GeometryNodeObjectInfo')
            obj_info.name = node_name
        obj_info.label = f"Proxy {role}"
        try:
            base_loc = colliders_group_node.location
            obj_info.location = (base_loc[0], base_loc[1] + role_y[role])
        except Exception:
            pass

        obj_socket = _find_socket_by_names(obj_info.inputs, {"Object"})
        if obj_socket is not None:
            try:
                obj_socket.default_value = proxy_obj
            except Exception as e:
                log.debug("Could not assign ClothSimulation proxy object %s (%s): %s", role, proxy_obj.name, e)
        as_instance_socket = _find_socket_by_names(obj_info.inputs, {"As Instance"})
        if as_instance_socket is not None:
            try:
                as_instance_socket.default_value = False
            except Exception:
                pass
        if hasattr(obj_info, "transform_space"):
            try:
                obj_info.transform_space = 'RELATIVE'
            except Exception:
                pass

        out_sock = _first_geometry_output(obj_info)
        if out_sock is None:
            continue
        try:
            links.new(out_sock, join_input)
            wired_any_proxy = True
        except Exception as e:
            log.debug("Failed linking ClothSimulation proxy %s into join node: %s", role, e)

    if not wired_any_proxy:
        return False

    # Replace all downstream consumers of Group.008 output with the proxy join output.
    for link in outgoing_links:
        try:
            links.remove(link)
        except Exception:
            pass
    for to_socket in outgoing_targets:
        try:
            links.new(join_output, to_socket)
        except Exception as e:
            log.debug("Failed rewiring ClothSimulation collider output to proxy join: %s", e)

    # Keep the APX colliders group node in place (for compatibility / easier inspection) but mute it.
    try:
        colliders_group_node.mute = True
        colliders_group_node.label = "APX Colliders (Bypassed by Witcher Proxy Patch)"
    except Exception:
        pass
    return True


def _find_gn_group_node(node_group, node_name: str = "", subtree_name_contains: str = ""):
    if node_group is None:
        return None
    node_name = (node_name or "").lower()
    subtree_name_contains = (subtree_name_contains or "").lower()
    for node in node_group.nodes:
        if node.bl_idname != 'GeometryNodeGroup':
            continue
        if node_name and (getattr(node, "name", "") or "").lower() == node_name:
            return node
        if subtree_name_contains:
            sub_tree = getattr(node, "node_tree", None)
            sub_name = (getattr(sub_tree, "name", "") or "").lower()
            if subtree_name_contains in sub_name:
                return node
    return None


def _find_top_level_node(node_group, bl_idname: str, node_name: str = ""):
    if node_group is None:
        return None
    node_name_l = (node_name or "").lower()
    for node in node_group.nodes:
        if node.bl_idname != bl_idname:
            continue
        if not node_name_l or (getattr(node, "name", "") or "").lower() == node_name_l:
            return node
    return None


def _rewire_input_socket(links, input_socket, from_socket) -> bool:
    if input_socket is None or from_socket is None:
        return False
    for lnk in list(input_socket.links):
        try:
            links.remove(lnk)
        except Exception:
            pass
    try:
        links.new(from_socket, input_socket)
        return True
    except Exception:
        return False


def _ensure_clothsim_physical_source_index_attr(node_group, attr_name: str = "witcher_src_vert_idx"):
    """Patch the APX physical-mesh builder (inside Softbody.Init.Cloth) to store a source vertex index.

    This is evaluated once through the APX Bake path and lets us sample live skinned positions cheaply later.
    """
    if node_group is None:
        return None

    init_cloth_node = _find_gn_group_node(
        node_group,
        node_name="Group.005",
        subtree_name_contains="softbody.init.cloth",
    )
    init_cloth_tree = getattr(init_cloth_node, "node_tree", None) if init_cloth_node else None
    if init_cloth_tree is None:
        return None

    phys_builder_node = _find_gn_group_node(init_cloth_tree, node_name="Group.005")
    if phys_builder_node is None:
        for node in init_cloth_tree.nodes:
            if node.bl_idname != 'GeometryNodeGroup':
                continue
            if _find_socket_by_names(getattr(node, "outputs", []), {"Full Physical Mesh"}) is not None and \
               _find_socket_by_names(getattr(node, "outputs", []), {"Simulated Mesh"}) is not None:
                phys_builder_node = node
                break
    phys_builder_tree = getattr(phys_builder_node, "node_tree", None) if phys_builder_node else None
    if phys_builder_tree is None:
        return None

    nodes = phys_builder_tree.nodes
    links = phys_builder_tree.links
    triangulate_node = nodes.get("Triangulate")
    group_input_node = nodes.get("Group Input")
    if triangulate_node is None or group_input_node is None:
        # Fallback to older patch point if APX variant differs.
        separate_node = nodes.get("Separate Geometry")
        merge_node = nodes.get("Merge by Distance")
        if separate_node is None or merge_node is None:
            return None
    else:
        separate_node = None
        merge_node = None

    idx_node = nodes.get("WitcherAPXSrcVertIndex")
    if idx_node is None or idx_node.bl_idname != 'GeometryNodeInputIndex':
        if idx_node is not None:
            nodes.remove(idx_node)
        idx_node = nodes.new('GeometryNodeInputIndex')
        idx_node.name = "WitcherAPXSrcVertIndex"
    store_node = nodes.get("WitcherAPXStoreSrcVertIndex")
    if store_node is None or store_node.bl_idname != 'GeometryNodeStoreNamedAttribute':
        if store_node is not None:
            nodes.remove(store_node)
        store_node = nodes.new('GeometryNodeStoreNamedAttribute')
        store_node.name = "WitcherAPXStoreSrcVertIndex"

    try:
        store_node.data_type = 'INT'
    except Exception:
        pass
    try:
        store_node.domain = 'POINT'
    except Exception:
        pass
    try:
        if len(store_node.inputs) > 1:
            store_node.inputs[1].default_value = True
    except Exception:
        pass
    try:
        if len(store_node.inputs) > 2:
            store_node.inputs[2].default_value = attr_name
    except Exception:
        pass
    store_geom_in = _find_socket_by_names(store_node.inputs, {"Geometry"}) or (store_node.inputs[0] if store_node.inputs else None)
    store_val_in = _find_socket_by_names(store_node.inputs, {"Value"}) or (store_node.inputs[3] if len(store_node.inputs) > 3 else None)
    store_geom_out = _first_geometry_output(store_node)
    if store_geom_in is None or store_val_in is None or store_geom_out is None:
        return None

    # Preferred path: compute source vertex mapping on the final triangulated physical mesh (more stable than pre-merge).
    if triangulate_node is not None and group_input_node is not None:
        nearest_node = nodes.get("WitcherAPXSrcVertNearest")
        if nearest_node is None or nearest_node.bl_idname != 'GeometryNodeSampleNearest':
            if nearest_node is not None:
                nodes.remove(nearest_node)
            nearest_node = nodes.new('GeometryNodeSampleNearest')
            nearest_node.name = "WitcherAPXSrcVertNearest"
        try:
            nearest_node.domain = 'POINT'
        except Exception:
            pass

        pos_node = nodes.get("WitcherAPXSrcVertNearestPos")
        if pos_node is None or pos_node.bl_idname != 'GeometryNodeInputPosition':
            if pos_node is not None:
                nodes.remove(pos_node)
            pos_node = nodes.new('GeometryNodeInputPosition')
            pos_node.name = "WitcherAPXSrcVertNearestPos"

        tri_geom_out = _first_geometry_output(triangulate_node)
        src_geom_out = _find_socket_by_names(group_input_node.outputs, {"Geometry"}) or (group_input_node.outputs[0] if group_input_node.outputs else None)
        pos_out = _find_socket_by_names(pos_node.outputs, {"Position"}) or (pos_node.outputs[0] if pos_node.outputs else None)
        near_geom_in = _find_socket_by_names(nearest_node.inputs, {"Mesh", "Geometry"}) or (nearest_node.inputs[0] if nearest_node.inputs else None)
        near_pos_in = _find_socket_by_names(nearest_node.inputs, {"Sample Position", "Position", "Value"}) or (nearest_node.inputs[1] if len(nearest_node.inputs) > 1 else None)
        near_idx_out = _find_socket_by_names(nearest_node.outputs, {"Index", "Value"}) or (nearest_node.outputs[0] if nearest_node.outputs else None)

        if all([tri_geom_out, src_geom_out, pos_out, near_geom_in, near_pos_in, near_idx_out]):
            # Wire nearest-sample fields.
            _rewire_input_socket(links, near_geom_in, src_geom_out)
            _rewire_input_socket(links, near_pos_in, pos_out)
            _rewire_input_socket(links, store_geom_in, tri_geom_out)
            _rewire_input_socket(links, store_val_in, near_idx_out)

            # Replace downstream consumers of Triangulate with Store Named Attribute output.
            outgoing_links = [lnk for lnk in list(tri_geom_out.links) if lnk.to_socket != store_geom_in]
            outgoing_targets = [lnk.to_socket for lnk in outgoing_links]
            for lnk in outgoing_links:
                try:
                    links.remove(lnk)
                except Exception:
                    pass
            for to_socket in outgoing_targets:
                try:
                    links.new(store_geom_out, to_socket)
                except Exception:
                    pass

            try:
                sx, sy = triangulate_node.location
                store_node.location = (sx + 180.0, sy - 20.0)
                nearest_node.location = (sx - 200.0, sy - 220.0)
                pos_node.location = (sx - 410.0, sy - 230.0)
                idx_node.location = (sx - 410.0, sy - 380.0)  # keep legacy helper node out of the way
            except Exception:
                pass
            try:
                store_node.label = "Witcher Source Vert Index (Baked, final phys mesh)"
            except Exception:
                pass
            return attr_name

    # Fallback: older pre-merge index path.
    if separate_node is None or merge_node is None:
        return None

    sep_geom_out = _first_geometry_output(separate_node)
    merge_geom_in = _first_geometry_input(merge_node)
    idx_out = _find_socket_by_names(idx_node.outputs, {"Index"}) or (idx_node.outputs[0] if idx_node.outputs else None)
    if not all([sep_geom_out, merge_geom_in, idx_out]):
        return None

    _rewire_input_socket(links, store_geom_in, sep_geom_out)
    _rewire_input_socket(links, store_val_in, idx_out)
    if not _rewire_input_socket(links, merge_geom_in, store_geom_out):
        return None

    try:
        sx, sy = separate_node.location
        store_node.location = (sx + 180.0, sy - 20.0)
        idx_node.location = (sx + 10.0, sy - 210.0)
    except Exception:
        pass
    try:
        store_node.label = "Witcher Source Vert Index (Baked, fallback)"
    except Exception:
        pass
    return attr_name


def _patch_clothsim_add_live_armature_pose(node_group) -> bool:
    """Add a fast live armature-driven pose path while preserving APX Bake/static setup."""
    if node_group is None:
        return False

    links = node_group.links
    nodes = node_group.nodes

    group_input_node = _find_top_level_node(node_group, 'NodeGroupInput', node_name="Group Input")
    sim_input_node = _find_top_level_node(node_group, 'GeometryNodeSimulationInput', node_name="Simulation Input")
    step_cloth_node = _find_gn_group_node(node_group, node_name="Group.003", subtree_name_contains="softbody.step.cloth")
    update_graphical_node = _find_gn_group_node(node_group, node_name="Group", subtree_name_contains="updategraphicalmesh")
    grab_selection_node = _find_gn_group_node(node_group, node_name="Group.006", subtree_name_contains="grabselection")

    if not group_input_node or not sim_input_node or not step_cloth_node:
        return False

    src_geom_out = _find_socket_by_names(group_input_node.outputs, {"Geometry"}) or (group_input_node.outputs[0] if group_input_node.outputs else None)
    sim_phys_out = _find_socket_by_names(sim_input_node.outputs, {"Physical Mesh"}) or (sim_input_node.outputs[1] if len(sim_input_node.outputs) > 1 else None)
    sim_graph_out = _find_socket_by_names(sim_input_node.outputs, {"Graphical Mesh"}) or (sim_input_node.outputs[2] if len(sim_input_node.outputs) > 2 else None)
    step_phys_in = _find_socket_by_names(step_cloth_node.inputs, {"Physical Mesh"}) or (step_cloth_node.inputs[0] if len(step_cloth_node.inputs) > 0 else None)

    if src_geom_out is None or sim_phys_out is None or step_phys_in is None:
        return False

    # Ensure the APX init/bake path stores a source-vertex mapping on the baked physical mesh.
    phys_src_attr_name = _ensure_clothsim_physical_source_index_attr(node_group)

    # Build/reuse shared field nodes.
    idx_node = nodes.get("WitcherAPXLivePoseIndex")
    if idx_node is None or idx_node.bl_idname != 'GeometryNodeInputIndex':
        if idx_node is not None:
            nodes.remove(idx_node)
        idx_node = nodes.new('GeometryNodeInputIndex')
        idx_node.name = "WitcherAPXLivePoseIndex"

    pos_node = nodes.get("WitcherAPXLivePosePosition")
    if pos_node is None or pos_node.bl_idname != 'GeometryNodeInputPosition':
        if pos_node is not None:
            nodes.remove(pos_node)
        pos_node = nodes.new('GeometryNodeInputPosition')
        pos_node.name = "WitcherAPXLivePosePosition"

    sim_attr_node = nodes.get("WitcherAPXLivePoseSimulatedAttr")
    if sim_attr_node is None or sim_attr_node.bl_idname != 'GeometryNodeInputNamedAttribute':
        if sim_attr_node is not None:
            nodes.remove(sim_attr_node)
        sim_attr_node = nodes.new('GeometryNodeInputNamedAttribute')
        sim_attr_node.name = "WitcherAPXLivePoseSimulatedAttr"
    try:
        sim_attr_node.data_type = 'BOOLEAN'
    except Exception:
        pass
    try:
        sim_attr_node.inputs[0].default_value = "simulated"
    except Exception:
        pass

    not_node = nodes.get("WitcherAPXLivePoseNot")
    if not_node is None or not_node.bl_idname != 'FunctionNodeBooleanMath':
        if not_node is not None:
            nodes.remove(not_node)
        not_node = nodes.new('FunctionNodeBooleanMath')
        not_node.name = "WitcherAPXLivePoseNot"
    try:
        not_node.operation = 'NOT'
    except Exception:
        pass

    # Physical-mesh live pose injection (non-simulated verts only).
    phys_maxdist_attr_node = nodes.get("WitcherAPXLivePosePhysMaxDistAttr")
    if phys_maxdist_attr_node is None or phys_maxdist_attr_node.bl_idname != 'GeometryNodeInputNamedAttribute':
        if phys_maxdist_attr_node is not None:
            nodes.remove(phys_maxdist_attr_node)
        phys_maxdist_attr_node = nodes.new('GeometryNodeInputNamedAttribute')
        phys_maxdist_attr_node.name = "WitcherAPXLivePosePhysMaxDistAttr"
    try:
        phys_maxdist_attr_node.data_type = 'FLOAT'
    except Exception:
        pass
    try:
        phys_maxdist_attr_node.inputs[0].default_value = "PhysXMaximumDistanceScaled"
    except Exception:
        pass

    phys_sim_cmp_node = nodes.get("WitcherAPXLivePosePhysSimulatedCmp")
    if phys_sim_cmp_node is None or phys_sim_cmp_node.bl_idname != 'FunctionNodeCompare':
        if phys_sim_cmp_node is not None:
            nodes.remove(phys_sim_cmp_node)
        phys_sim_cmp_node = nodes.new('FunctionNodeCompare')
        phys_sim_cmp_node.name = "WitcherAPXLivePosePhysSimulatedCmp"
    try:
        phys_sim_cmp_node.data_type = 'FLOAT'
        phys_sim_cmp_node.mode = 'ELEMENT'
        phys_sim_cmp_node.operation = 'GREATER_THAN'
    except Exception:
        pass
    try:
        # Float compare "B" is usually input 1.
        if len(phys_sim_cmp_node.inputs) > 1:
            phys_sim_cmp_node.inputs[1].default_value = 0.0
    except Exception:
        pass

    phys_not_node = nodes.get("WitcherAPXLivePosePhysNot")
    if phys_not_node is None or phys_not_node.bl_idname != 'FunctionNodeBooleanMath':
        if phys_not_node is not None:
            nodes.remove(phys_not_node)
        phys_not_node = nodes.new('FunctionNodeBooleanMath')
        phys_not_node.name = "WitcherAPXLivePosePhysNot"
    try:
        phys_not_node.operation = 'NOT'
    except Exception:
        pass

    phys_src_attr_node = nodes.get("WitcherAPXLivePosePhysSourceVertAttr")
    if phys_src_attr_node is None or phys_src_attr_node.bl_idname != 'GeometryNodeInputNamedAttribute':
        if phys_src_attr_node is not None:
            nodes.remove(phys_src_attr_node)
        phys_src_attr_node = nodes.new('GeometryNodeInputNamedAttribute')
        phys_src_attr_node.name = "WitcherAPXLivePosePhysSourceVertAttr"
    try:
        phys_src_attr_node.data_type = 'INT'
    except Exception:
        pass
    try:
        phys_src_attr_node.inputs[0].default_value = phys_src_attr_name or "witcher_src_vert_idx"
    except Exception:
        pass

    phys_sample_node = nodes.get("WitcherAPXLivePosePhysSample")
    if phys_sample_node is None or phys_sample_node.bl_idname != 'GeometryNodeSampleIndex':
        if phys_sample_node is not None:
            nodes.remove(phys_sample_node)
        phys_sample_node = nodes.new('GeometryNodeSampleIndex')
        phys_sample_node.name = "WitcherAPXLivePosePhysSample"
    try:
        phys_sample_node.clamp = False
    except Exception:
        pass
    try:
        phys_sample_node.data_type = 'FLOAT_VECTOR'
    except Exception:
        pass
    try:
        phys_sample_node.domain = 'POINT'
    except Exception:
        pass

    src_normal_node = nodes.get("WitcherAPXLivePoseSourceNormal")
    if src_normal_node is None or src_normal_node.bl_idname != 'GeometryNodeInputNormal':
        if src_normal_node is not None:
            nodes.remove(src_normal_node)
        src_normal_node = nodes.new('GeometryNodeInputNormal')
        src_normal_node.name = "WitcherAPXLivePoseSourceNormal"
    try:
        # Blender API compatibility (property exists on some versions).
        src_normal_node.legacy_corner_normals = True
    except Exception:
        pass

    phys_normal_sample_node = nodes.get("WitcherAPXLivePosePhysNormalSample")
    if phys_normal_sample_node is None or phys_normal_sample_node.bl_idname != 'GeometryNodeSampleIndex':
        if phys_normal_sample_node is not None:
            nodes.remove(phys_normal_sample_node)
        phys_normal_sample_node = nodes.new('GeometryNodeSampleIndex')
        phys_normal_sample_node.name = "WitcherAPXLivePosePhysNormalSample"
    try:
        phys_normal_sample_node.clamp = False
    except Exception:
        pass
    try:
        phys_normal_sample_node.data_type = 'FLOAT_VECTOR'
    except Exception:
        pass
    try:
        phys_normal_sample_node.domain = 'POINT'
    except Exception:
        pass

    phys_normalize_node = nodes.get("WitcherAPXLivePosePhysNormalNormalize")
    if phys_normalize_node is None or phys_normalize_node.bl_idname != 'ShaderNodeVectorMath':
        if phys_normalize_node is not None:
            nodes.remove(phys_normalize_node)
        phys_normalize_node = nodes.new('ShaderNodeVectorMath')
        phys_normalize_node.name = "WitcherAPXLivePosePhysNormalNormalize"
    try:
        phys_normalize_node.operation = 'NORMALIZE'
    except Exception:
        pass

    setpos_node = nodes.get("WitcherAPXLivePoseSetPosition")
    if setpos_node is None or setpos_node.bl_idname != 'GeometryNodeSetPosition':
        if setpos_node is not None:
            nodes.remove(setpos_node)
        setpos_node = nodes.new('GeometryNodeSetPosition')
        setpos_node.name = "WitcherAPXLivePoseSetPosition"
    try:
        setpos_node.inputs[3].default_value = (0.0, 0.0, 0.0)
    except Exception:
        pass

    phys_store_pinned_pos_node = nodes.get("WitcherAPXLivePoseStorePinnedPosition")
    if phys_store_pinned_pos_node is None or phys_store_pinned_pos_node.bl_idname != 'GeometryNodeStoreNamedAttribute':
        if phys_store_pinned_pos_node is not None:
            nodes.remove(phys_store_pinned_pos_node)
        phys_store_pinned_pos_node = nodes.new('GeometryNodeStoreNamedAttribute')
        phys_store_pinned_pos_node.name = "WitcherAPXLivePoseStorePinnedPosition"
    try:
        phys_store_pinned_pos_node.data_type = 'FLOAT_VECTOR'
        phys_store_pinned_pos_node.domain = 'POINT'
    except Exception:
        pass
    try:
        phys_store_pinned_pos_node.inputs[1].default_value = True
        phys_store_pinned_pos_node.inputs[2].default_value = "pinned_position"
    except Exception:
        pass

    phys_store_pinned_norm_node = nodes.get("WitcherAPXLivePoseStorePinnedNormal")
    if phys_store_pinned_norm_node is None or phys_store_pinned_norm_node.bl_idname != 'GeometryNodeStoreNamedAttribute':
        if phys_store_pinned_norm_node is not None:
            nodes.remove(phys_store_pinned_norm_node)
        phys_store_pinned_norm_node = nodes.new('GeometryNodeStoreNamedAttribute')
        phys_store_pinned_norm_node.name = "WitcherAPXLivePoseStorePinnedNormal"
    try:
        phys_store_pinned_norm_node.data_type = 'FLOAT_VECTOR'
        phys_store_pinned_norm_node.domain = 'POINT'
    except Exception:
        pass
    try:
        phys_store_pinned_norm_node.inputs[1].default_value = True
        phys_store_pinned_norm_node.inputs[2].default_value = "pinned_normal"
    except Exception:
        pass

    phys_store_old_pos_node = nodes.get("WitcherAPXLivePoseStoreOldPosition")
    if phys_store_old_pos_node is None or phys_store_old_pos_node.bl_idname != 'GeometryNodeStoreNamedAttribute':
        if phys_store_old_pos_node is not None:
            nodes.remove(phys_store_old_pos_node)
        phys_store_old_pos_node = nodes.new('GeometryNodeStoreNamedAttribute')
        phys_store_old_pos_node.name = "WitcherAPXLivePoseStoreOldPosition"
    try:
        phys_store_old_pos_node.data_type = 'FLOAT_VECTOR'
        phys_store_old_pos_node.domain = 'POINT'
    except Exception:
        pass
    try:
        phys_store_old_pos_node.inputs[1].default_value = True
        phys_store_old_pos_node.inputs[2].default_value = "old_position"
    except Exception:
        pass

    phys_store_vel_node = nodes.get("WitcherAPXLivePoseStoreVelocity")
    if phys_store_vel_node is None or phys_store_vel_node.bl_idname != 'GeometryNodeStoreNamedAttribute':
        if phys_store_vel_node is not None:
            nodes.remove(phys_store_vel_node)
        phys_store_vel_node = nodes.new('GeometryNodeStoreNamedAttribute')
        phys_store_vel_node.name = "WitcherAPXLivePoseStoreVelocity"
    try:
        phys_store_vel_node.data_type = 'FLOAT_VECTOR'
        phys_store_vel_node.domain = 'POINT'
    except Exception:
        pass
    try:
        phys_store_vel_node.inputs[1].default_value = True
        phys_store_vel_node.inputs[2].default_value = "velocity"
        # Zero velocity for non-simulated verts when we snap them to the live rig pose.
        phys_store_vel_node.inputs[3].default_value = (0.0, 0.0, 0.0)
    except Exception:
        pass

    # Graphical-mesh live pose update for non-simulated verts (keeps baked APX mapping attrs).
    graph_sample_node = nodes.get("WitcherAPXLivePoseGraphSample")
    if graph_sample_node is None or graph_sample_node.bl_idname != 'GeometryNodeSampleIndex':
        if graph_sample_node is not None:
            nodes.remove(graph_sample_node)
        graph_sample_node = nodes.new('GeometryNodeSampleIndex')
        graph_sample_node.name = "WitcherAPXLivePoseGraphSample"
    try:
        graph_sample_node.clamp = False
    except Exception:
        pass
    try:
        graph_sample_node.data_type = 'FLOAT_VECTOR'
    except Exception:
        pass
    try:
        graph_sample_node.domain = 'POINT'
    except Exception:
        pass

    graph_setpos_node = nodes.get("WitcherAPXLivePoseGraphSetPosition")
    if graph_setpos_node is None or graph_setpos_node.bl_idname != 'GeometryNodeSetPosition':
        if graph_setpos_node is not None:
            nodes.remove(graph_setpos_node)
        graph_setpos_node = nodes.new('GeometryNodeSetPosition')
        graph_setpos_node.name = "WitcherAPXLivePoseGraphSetPosition"
    try:
        graph_setpos_node.inputs[3].default_value = (0.0, 0.0, 0.0)
    except Exception:
        pass

    # Layout near APX solver nodes.
    try:
        sx, sy = step_cloth_node.location
        setpos_node.location = (sx - 520.0, sy + 40.0)
        phys_store_pinned_pos_node.location = (sx - 280.0, sy + 30.0)
        phys_store_pinned_norm_node.location = (sx - 40.0, sy + 30.0)
        phys_store_old_pos_node.location = (sx + 200.0, sy + 30.0)
        phys_store_vel_node.location = (sx + 440.0, sy + 30.0)

        phys_sample_node.location = (sx - 780.0, sy + 20.0)
        phys_normal_sample_node.location = (sx - 780.0, sy + 190.0)
        phys_normalize_node.location = (sx - 560.0, sy + 200.0)
        src_normal_node.location = (sx - 1030.0, sy + 170.0)
        phys_src_attr_node.location = (sx - 1030.0, sy - 80.0)
        phys_maxdist_attr_node.location = (sx - 1030.0, sy + 10.0)
        phys_sim_cmp_node.location = (sx - 840.0, sy - 120.0)
        phys_not_node.location = (sx - 650.0, sy - 120.0)
        idx_node.location = (sx - 760.0, sy + 310.0)
        pos_node.location = (sx - 760.0, sy + 120.0)
        sim_attr_node.location = (sx - 530.0, sy + 230.0)
        not_node.location = (sx - 360.0, sy + 230.0)
        graph_sample_node.location = (sx - 520.0, sy + 350.0)
        graph_setpos_node.location = (sx - 280.0, sy + 370.0)
    except Exception:
        pass

    phys_sample_geom_in = _find_socket_by_names(phys_sample_node.inputs, {"Geometry"}) or (phys_sample_node.inputs[0] if len(phys_sample_node.inputs) > 0 else None)
    phys_sample_val_in = _find_socket_by_names(phys_sample_node.inputs, {"Value"}) or (phys_sample_node.inputs[1] if len(phys_sample_node.inputs) > 1 else None)
    phys_sample_idx_in = _find_socket_by_names(phys_sample_node.inputs, {"Index"}) or (phys_sample_node.inputs[2] if len(phys_sample_node.inputs) > 2 else None)
    phys_sample_val_out = _find_socket_by_names(phys_sample_node.outputs, {"Value"}) or (phys_sample_node.outputs[0] if phys_sample_node.outputs else None)
    phys_normal_sample_geom_in = _find_socket_by_names(phys_normal_sample_node.inputs, {"Geometry"}) or (phys_normal_sample_node.inputs[0] if len(phys_normal_sample_node.inputs) > 0 else None)
    phys_normal_sample_val_in = _find_socket_by_names(phys_normal_sample_node.inputs, {"Value"}) or (phys_normal_sample_node.inputs[1] if len(phys_normal_sample_node.inputs) > 1 else None)
    phys_normal_sample_idx_in = _find_socket_by_names(phys_normal_sample_node.inputs, {"Index"}) or (phys_normal_sample_node.inputs[2] if len(phys_normal_sample_node.inputs) > 2 else None)
    phys_normal_sample_val_out = _find_socket_by_names(phys_normal_sample_node.outputs, {"Value"}) or (phys_normal_sample_node.outputs[0] if phys_normal_sample_node.outputs else None)

    setpos_geom_in = _find_socket_by_names(setpos_node.inputs, {"Geometry"}) or (setpos_node.inputs[0] if len(setpos_node.inputs) > 0 else None)
    setpos_sel_in = _find_socket_by_names(setpos_node.inputs, {"Selection"}) or (setpos_node.inputs[1] if len(setpos_node.inputs) > 1 else None)
    setpos_pos_in = _find_socket_by_names(setpos_node.inputs, {"Position"}) or (setpos_node.inputs[2] if len(setpos_node.inputs) > 2 else None)
    setpos_geom_out = _first_geometry_output(setpos_node)

    phys_store_pinned_pos_geom_in = _find_socket_by_names(phys_store_pinned_pos_node.inputs, {"Geometry"}) or (phys_store_pinned_pos_node.inputs[0] if len(phys_store_pinned_pos_node.inputs) > 0 else None)
    phys_store_pinned_pos_val_in = _find_socket_by_names(phys_store_pinned_pos_node.inputs, {"Value"}) or (phys_store_pinned_pos_node.inputs[3] if len(phys_store_pinned_pos_node.inputs) > 3 else None)
    phys_store_pinned_pos_out = _first_geometry_output(phys_store_pinned_pos_node)
    phys_store_pinned_norm_geom_in = _find_socket_by_names(phys_store_pinned_norm_node.inputs, {"Geometry"}) or (phys_store_pinned_norm_node.inputs[0] if len(phys_store_pinned_norm_node.inputs) > 0 else None)
    phys_store_pinned_norm_val_in = _find_socket_by_names(phys_store_pinned_norm_node.inputs, {"Value"}) or (phys_store_pinned_norm_node.inputs[3] if len(phys_store_pinned_norm_node.inputs) > 3 else None)
    phys_store_pinned_norm_out = _first_geometry_output(phys_store_pinned_norm_node)
    phys_store_old_pos_geom_in = _find_socket_by_names(phys_store_old_pos_node.inputs, {"Geometry"}) or (phys_store_old_pos_node.inputs[0] if len(phys_store_old_pos_node.inputs) > 0 else None)
    phys_store_old_pos_sel_in = _find_socket_by_names(phys_store_old_pos_node.inputs, {"Selection"}) or (phys_store_old_pos_node.inputs[1] if len(phys_store_old_pos_node.inputs) > 1 else None)
    phys_store_old_pos_val_in = _find_socket_by_names(phys_store_old_pos_node.inputs, {"Value"}) or (phys_store_old_pos_node.inputs[3] if len(phys_store_old_pos_node.inputs) > 3 else None)
    phys_store_old_pos_out = _first_geometry_output(phys_store_old_pos_node)
    phys_store_vel_geom_in = _find_socket_by_names(phys_store_vel_node.inputs, {"Geometry"}) or (phys_store_vel_node.inputs[0] if len(phys_store_vel_node.inputs) > 0 else None)
    phys_store_vel_sel_in = _find_socket_by_names(phys_store_vel_node.inputs, {"Selection"}) or (phys_store_vel_node.inputs[1] if len(phys_store_vel_node.inputs) > 1 else None)
    phys_store_vel_out = _first_geometry_output(phys_store_vel_node)

    phys_attr_out = _find_socket_by_names(phys_src_attr_node.outputs, {"Attribute"}) or (phys_src_attr_node.outputs[0] if phys_src_attr_node.outputs else None)
    phys_maxdist_attr_out = _find_socket_by_names(phys_maxdist_attr_node.outputs, {"Attribute"}) or (phys_maxdist_attr_node.outputs[0] if phys_maxdist_attr_node.outputs else None)
    phys_cmp_a_in = _find_socket_by_names(phys_sim_cmp_node.inputs, {"A"}) or (phys_sim_cmp_node.inputs[0] if phys_sim_cmp_node.inputs else None)
    phys_cmp_out = _find_socket_by_names(phys_sim_cmp_node.outputs, {"Result"}) or (phys_sim_cmp_node.outputs[0] if phys_sim_cmp_node.outputs else None)
    phys_not_in = _find_socket_by_names(phys_not_node.inputs, {"Boolean"}) or (phys_not_node.inputs[0] if phys_not_node.inputs else None)
    phys_not_out = _find_socket_by_names(phys_not_node.outputs, {"Boolean"}) or (phys_not_node.outputs[0] if phys_not_node.outputs else None)
    src_normal_out = _find_socket_by_names(src_normal_node.outputs, {"Normal"}) or (src_normal_node.outputs[0] if src_normal_node.outputs else None)
    phys_normalize_in = _find_socket_by_names(phys_normalize_node.inputs, {"Vector"}) or (phys_normalize_node.inputs[0] if phys_normalize_node.inputs else None)
    phys_normalize_out = _find_socket_by_names(phys_normalize_node.outputs, {"Vector"}) or (phys_normalize_node.outputs[0] if phys_normalize_node.outputs else None)

    if not all([
        phys_sample_geom_in, phys_sample_val_in, phys_sample_idx_in, phys_sample_val_out,
        phys_normal_sample_geom_in, phys_normal_sample_val_in, phys_normal_sample_idx_in, phys_normal_sample_val_out,
        setpos_geom_in, setpos_sel_in, setpos_pos_in, setpos_geom_out,
        phys_store_pinned_pos_geom_in, phys_store_pinned_pos_val_in, phys_store_pinned_pos_out,
        phys_store_pinned_norm_geom_in, phys_store_pinned_norm_val_in, phys_store_pinned_norm_out,
        phys_store_old_pos_geom_in, phys_store_old_pos_sel_in, phys_store_old_pos_val_in, phys_store_old_pos_out,
        phys_store_vel_geom_in, phys_store_vel_sel_in, phys_store_vel_out,
        phys_attr_out, phys_maxdist_attr_out, phys_cmp_a_in, phys_cmp_out, phys_not_in, phys_not_out,
        src_normal_out, phys_normalize_in, phys_normalize_out,
    ]):
        return False

    pos_out = _find_socket_by_names(pos_node.outputs, {"Position"}) or (pos_node.outputs[0] if pos_node.outputs else None)
    idx_out = _find_socket_by_names(idx_node.outputs, {"Index"}) or (idx_node.outputs[0] if idx_node.outputs else None)
    not_out = _find_socket_by_names(not_node.outputs, {"Boolean"}) or (not_node.outputs[0] if not_node.outputs else None)
    sim_attr_out = _find_socket_by_names(sim_attr_node.outputs, {"Attribute"}) or (sim_attr_node.outputs[0] if sim_attr_node.outputs else None)
    not_in = _find_socket_by_names(not_node.inputs, {"Boolean"}) or (not_node.inputs[0] if not_node.inputs else None)

    if pos_out is None or idx_out is None or not_out is None or sim_attr_out is None or not_in is None:
        return False

    # Fast path: sample live positions from modifier input geometry using a baked source-index attr on physical mesh.
    if phys_src_attr_name:
        _rewire_input_socket(links, phys_sample_geom_in, src_geom_out)
        _rewire_input_socket(links, phys_sample_val_in, pos_out)
        _rewire_input_socket(links, phys_sample_idx_in, phys_attr_out)
        _rewire_input_socket(links, phys_normal_sample_geom_in, src_geom_out)
        _rewire_input_socket(links, phys_normal_sample_val_in, src_normal_out)
        _rewire_input_socket(links, phys_normal_sample_idx_in, phys_attr_out)
    else:
        # Fallback (slower): sample from live Softbody.Init.Cloth if the source-index bake patch could not be applied.
        init_cloth_node = _find_gn_group_node(node_group, node_name="Group.005", subtree_name_contains="softbody.init.cloth")
        init_phys_out = _find_socket_by_names(getattr(init_cloth_node, "outputs", []), {"Physical Mesh"}) if init_cloth_node else None
        if init_phys_out is None and init_cloth_node and len(init_cloth_node.outputs) > 1:
            init_phys_out = init_cloth_node.outputs[1]
        if init_phys_out is None:
            return False
        _rewire_input_socket(links, phys_sample_geom_in, init_phys_out)
        _rewire_input_socket(links, phys_sample_val_in, pos_out)
        _rewire_input_socket(links, phys_sample_idx_in, idx_out)
        # No reliable physical->source map means we cannot safely update pinned normals/anchors.
        # Leave those attributes as APX-baked defaults in this fallback mode.

    _rewire_input_socket(links, setpos_geom_in, sim_phys_out)
    _rewire_input_socket(links, setpos_pos_in, phys_sample_val_out)
    _rewire_input_socket(links, phys_cmp_a_in, phys_maxdist_attr_out)
    _rewire_input_socket(links, phys_not_in, phys_cmp_out)
    _rewire_input_socket(links, setpos_sel_in, phys_not_out)
    _rewire_input_socket(links, not_in, sim_attr_out)

    # Normalize sampled source normals before writing pinned_normal.
    _rewire_input_socket(links, phys_normalize_in, phys_normal_sample_val_out)

    # Update APX anchor attributes per frame so armature motion moves cloth constraints, not just visible verts.
    phys_chain_out = setpos_geom_out
    if phys_src_attr_name:
        _rewire_input_socket(links, phys_store_pinned_pos_geom_in, setpos_geom_out)
        _rewire_input_socket(links, phys_store_pinned_pos_val_in, phys_sample_val_out)
        _rewire_input_socket(links, phys_store_pinned_norm_geom_in, phys_store_pinned_pos_out)
        _rewire_input_socket(links, phys_store_pinned_norm_val_in, phys_normalize_out)
        phys_chain_out = phys_store_pinned_norm_out

    _rewire_input_socket(links, phys_store_old_pos_geom_in, phys_chain_out)
    _rewire_input_socket(links, phys_store_old_pos_sel_in, phys_not_out)
    _rewire_input_socket(links, phys_store_old_pos_val_in, phys_sample_val_out)
    _rewire_input_socket(links, phys_store_vel_geom_in, phys_store_old_pos_out)
    _rewire_input_socket(links, phys_store_vel_sel_in, phys_not_out)

    patched_any = _rewire_input_socket(links, step_phys_in, phys_store_vel_out)

    # Update the baked graphical mesh positions for non-simulated verts using the live modifier input geometry.
    graph_sample_geom_in = _find_socket_by_names(graph_sample_node.inputs, {"Geometry"}) or (graph_sample_node.inputs[0] if len(graph_sample_node.inputs) > 0 else None)
    graph_sample_val_in = _find_socket_by_names(graph_sample_node.inputs, {"Value"}) or (graph_sample_node.inputs[1] if len(graph_sample_node.inputs) > 1 else None)
    graph_sample_idx_in = _find_socket_by_names(graph_sample_node.inputs, {"Index"}) or (graph_sample_node.inputs[2] if len(graph_sample_node.inputs) > 2 else None)
    graph_sample_val_out = _find_socket_by_names(graph_sample_node.outputs, {"Value"}) or (graph_sample_node.outputs[0] if graph_sample_node.outputs else None)
    graph_setpos_geom_in = _find_socket_by_names(graph_setpos_node.inputs, {"Geometry"}) or (graph_setpos_node.inputs[0] if len(graph_setpos_node.inputs) > 0 else None)
    graph_setpos_sel_in = _find_socket_by_names(graph_setpos_node.inputs, {"Selection"}) or (graph_setpos_node.inputs[1] if len(graph_setpos_node.inputs) > 1 else None)
    graph_setpos_pos_in = _find_socket_by_names(graph_setpos_node.inputs, {"Position"}) or (graph_setpos_node.inputs[2] if len(graph_setpos_node.inputs) > 2 else None)
    graph_setpos_out = _first_geometry_output(graph_setpos_node)

    live_graph_out = sim_graph_out
    if all([sim_graph_out, graph_sample_geom_in, graph_sample_val_in, graph_sample_idx_in, graph_sample_val_out,
            graph_setpos_geom_in, graph_setpos_sel_in, graph_setpos_pos_in, graph_setpos_out]):
        _rewire_input_socket(links, graph_sample_geom_in, src_geom_out)
        _rewire_input_socket(links, graph_sample_val_in, pos_out)
        _rewire_input_socket(links, graph_sample_idx_in, idx_out)
        _rewire_input_socket(links, graph_setpos_geom_in, sim_graph_out)
        _rewire_input_socket(links, graph_setpos_pos_in, graph_sample_val_out)
        _rewire_input_socket(links, graph_setpos_sel_in, not_out)
        live_graph_out = graph_setpos_out
        patched_any = True

    # Feed the live-updated graphical mesh into display/grab paths (instead of frozen first-frame positions).
    if live_graph_out is not None:
        if update_graphical_node is not None:
            graph_in = _find_socket_by_names(update_graphical_node.inputs, {"Graphical Mesh"})
            if graph_in is None and len(update_graphical_node.inputs) > 1:
                graph_in = update_graphical_node.inputs[1]
            if graph_in is not None and _rewire_input_socket(links, graph_in, live_graph_out):
                patched_any = True

        if grab_selection_node is not None:
            grab_graph_in = _find_socket_by_names(grab_selection_node.inputs, {"Graphical Mesh"})
            if grab_graph_in is None and grab_selection_node.inputs:
                grab_graph_in = grab_selection_node.inputs[0]
            if grab_graph_in is not None and _rewire_input_socket(links, grab_graph_in, live_graph_out):
                patched_any = True

    if patched_any:
        try:
            if hasattr(setpos_node, "label"):
                setpos_node.label = "Witcher Live Pose Injection (Physical)"
            if hasattr(graph_setpos_node, "label"):
                graph_setpos_node.label = "Witcher Live Pose Injection (Graphical)"
            if hasattr(phys_store_pinned_pos_node, "label"):
                phys_store_pinned_pos_node.label = "Witcher Live pinned_position"
            if hasattr(phys_store_pinned_norm_node, "label"):
                phys_store_pinned_norm_node.label = "Witcher Live pinned_normal"
        except Exception:
            pass
    return patched_any


def _patch_clothsim_nodegroup_to_object_proxies(cloth_obj: Object, proxy_objects: Dict[str, Object]) -> bool:
    """Patch the copied APX ClothSimulation node group to use object proxies instead of collection inputs."""
    mod = _find_clothsimulation_modifier(cloth_obj)
    if mod is None or getattr(mod, "node_group", None) is None:
        return False

    node_group = mod.node_group
    patched_any = False

    # Some APX versions inline Collection Info nodes in the top-level ClothSimulation group.
    collection_nodes = [n for n in node_group.nodes if n.bl_idname == 'GeometryNodeCollectionInfo']
    if collection_nodes:
        remaining_roles = {k for k, v in proxy_objects.items() if v is not None}
        for coll_node in list(collection_nodes):
            role = _classify_collection_info_node(coll_node, mod=mod)
            if role is None and len(remaining_roles) == 1:
                role = next(iter(remaining_roles))
            proxy_obj = proxy_objects.get(role) if role else None
            if proxy_obj is None:
                continue
            if _replace_collection_info_node_with_object_info(node_group, coll_node, proxy_obj):
                patched_any = True
                if role in remaining_roles:
                    remaining_roles.remove(role)

    # APX template from your provided example stores colliders in nested Group.008 / SoftBody.Init.Colliders.
    if not patched_any:
        patched_any = _patch_clothsim_groupnode_colliders_to_proxies(node_group, proxy_objects)

    # Add a live armature-pose injection path while keeping the original APX static setup intact.
    live_pose_patched = _patch_clothsim_add_live_armature_pose(node_group)

    if patched_any:
        try:
            cloth_obj["witcher_apx_cloth_collision_mode"] = "object_proxy"
            cloth_obj["witcher_apx_cloth_node_group"] = node_group.name
            for role, proxy_obj in proxy_objects.items():
                if proxy_obj:
                    cloth_obj[f"witcher_apx_{role}_proxy"] = proxy_obj.name
        except Exception:
            pass
    if live_pose_patched:
        try:
            cloth_obj["witcher_apx_live_pose_patch"] = True
        except Exception:
            pass
    return patched_any

def color_to_weights(obj, src_vcol, src_channel_idx, dst_vgroup_idx):
    mesh = obj.data
    
    cols = []
    for col in src_vcol.data:
        cols.append(col)

    # build 2d array containing sum of color channel value, number of values
    # used to calculate average for vertex when setting weights
    vertex_values = [[0.0, 0] for i in range(0, len(mesh.vertices))]
    vertex_values1 = [[0.0, 0] for i in range(0, len(mesh.vertices))]
    vertex_values2 = [[0.0, 0] for i in range(0, len(mesh.vertices))]
    vertex_values3 = [[0.0, 0] for i in range(0, len(mesh.vertices))]
    
    for idx, vertex in enumerate(vertex_values):
        elem = src_vcol.data[idx]
        c = elem.color if hasattr(elem, 'color') else elem.vector
        vertex_values[idx][0] = c[1]
        vertex_values1[idx][0] = c[1]
        vertex_values2[idx][0] = c[2]
        vertex_values3[idx][0] = c[3]
    
    group = obj.vertex_groups[dst_vgroup_idx]
    mode = 'REPLACE'

    for i in range(0, len(mesh.vertices)):
        weight = vertex_values[i][0]
        # if weight == 0.0:
        #     group.add([i], weight, mode)
        # else:
        reverse = (1 - weight)
        reverse = reverse if reverse > 0.99 else reverse/2.5
        group.add([i], reverse, mode)

    mesh.update()
    
red_id = 'R'
green_id = 'G'
blue_id = 'B'
alpha_id = 'A'
def channel_id_to_idx(id):
    if id is red_id:
        return 0
    if id is green_id:
        return 1
    if id is blue_id:
        return 2
    if id is alpha_id:
        return 3
    # default to red
    return 0

import importlib
try:
    importlib.import_module("io_mesh_apx")
    addon_installed = True
except ImportError:
    addon_installed = False
    
try:
    importlib.import_module("io_scene_apx")
    older_addon_installed = True
except ImportError:
    older_addon_installed = False


def _addon_enabled(addon_id: str) -> bool:
    try:
        exists, enabled = addon_utils.check(addon_id)
    except Exception:
        return False
    return bool(exists and enabled)


def _io_mesh_apx_runtime_ready(context=None) -> bool:
    ctx = context or bpy.context
    wm = getattr(ctx, "window_manager", None)
    return wm is not None and hasattr(wm, "physx")


def importCloth(context, filepath, use_mat, rotate_180, rm_ph_me, mat_filename="", ns="cloth", name=":"):
    total_started = time.perf_counter()
    sanitize_seconds = 0.0
    addon_import_seconds = 0.0
    armature_scan_seconds = 0.0
    fix_tail_seconds = 0.0
    collision_seconds = 0.0
    mesh_scan_seconds = 0.0
    material_read_seconds = 0.0
    material_apply_seconds = 0.0
    runtime_defaults_seconds = 0.0
    patch_seconds = 0.0
    weights_seconds = 0.0
    merge_seconds = 0.0
    move_seconds = 0.0
    restore_seconds = 0.0
    proxy_count = 0
    gmesh_count = 0
    addon_name = "none"

    save_selected = bpy.context.selected_objects[:]
    save_active = bpy.context.view_layer.objects.active
    save_layer_collection = getattr(bpy.context.view_layer, "active_layer_collection", None)
    save_collection = getattr(save_layer_collection, "collection", None)
    
    if not context:
        context = bpy.context

    def _restore_import_context():
        try:
            bpy.context.view_layer.objects.active = None
        except Exception:
            pass
        try:
            bpy.ops.object.select_all(action='DESELECT')
        except Exception:
            pass
        try:
            bpy.context.view_layer.objects.active = save_active
        except Exception:
            pass
        for ob in save_selected:
            try:
                ob.select_set(True)
            except Exception:
                pass
        try:
            _restore_active_layer_collection_for_collection(context, save_collection)
        except Exception:
            pass

    import_snapshot = _snapshot_blender_import_state()

    # Global addon preference is the single source of truth.
    DO_WEAR_CLOTH = bool(get_DO_WEAR_CLOTH(context))

    if not filepath or not os.path.isfile(filepath):
        log.warning("Skipping redcloth import, APX/APB file not found: %s", filepath)
        return None

    uncook_path = get_texture_path(context)+"\\" # PATH WITH TEXTURES

    sanitize_started = time.perf_counter()
    filepath = _sanitize_apx_for_import(filepath)
    sanitize_seconds = time.perf_counter() - sanitize_started
    if sanitize_seconds >= _REDCLOTH_PROFILE_WARN_THRESHOLD:
        _log_redcloth_profile_warning(
            "sanitize %s %.3fs",
            os.path.basename(filepath),
            sanitize_seconds,
        )

    try:
        io_mesh_enabled = _addon_enabled("io_mesh_apx")
        legacy_enabled = _addon_enabled("io_scene_apx")
        io_mesh_runtime_ready = _io_mesh_apx_runtime_ready(context)

        if io_mesh_enabled and io_mesh_runtime_ready:
            addon_name = "io_mesh_apx"
            from io_mesh_apx.importer.import_clothing import read_clothing
            args_count = len(inspect.signature(read_clothing).parameters)
            if args_count == 4:
                addon_import_started = time.perf_counter()
                read_clothing(context, filepath, rotate_180, rm_ph_me)
                addon_import_seconds = time.perf_counter() - addon_import_started
            else:
                raise RuntimeError(f"Unsupported io_mesh_apx.read_clothing signature: {args_count}")
        elif legacy_enabled:
            addon_name = "io_scene_apx"
            from io_scene_apx.importer.import_clothing import read_clothing
            addon_import_started = time.perf_counter()
            read_clothing(context, filepath, use_mat, rotate_180, rm_ph_me)
            addon_import_seconds = time.perf_counter() - addon_import_started
        else:
            if io_mesh_enabled and not io_mesh_runtime_ready:
                log.warning(
                    "Skipping redcloth import for %s: io_mesh_apx is enabled but not runtime-ready in this Blender session "
                    "(WindowManager.physx missing).",
                    os.path.basename(filepath),
                )
            else:
                log.warning("Cloth plugin unavailable: enable io_mesh_apx (or legacy io_scene_apx)")
            return None
        if addon_import_seconds >= _REDCLOTH_PROFILE_WARN_THRESHOLD:
            _log_redcloth_profile_warning(
                "addon import %s %.3fs (addon %s)",
                os.path.basename(filepath),
                addon_import_seconds,
                addon_name,
            )
        # objs = bpy.context.objects[:]
        # for obj in objs:
        #     print (obj.name)

        #get the cloth mesh and select it
        bpy.context.view_layer.objects.active = None
        bpy.ops.object.select_all(action='DESELECT')
        active_layer_collection = getattr(bpy.context.view_layer, "active_layer_collection", None)
        active_coll = getattr(active_layer_collection, "collection", None) or save_collection
        if active_coll is None:
            raise RuntimeError(f"No active collection available after APX import for {filepath}")
        arma = None
        armature_scan_started = time.perf_counter()
        arma_objs = []
        for ob in active_coll.all_objects:
            if ob.type == "ARMATURE" and "Armature" in ob.name:
                arma_objs.append(ob)
        arma_objs.sort(key=lambda x: x.name, reverse=True)
        armature_scan_seconds = time.perf_counter() - armature_scan_started
        if not arma_objs:
            raise RuntimeError(f"No APX armature found after import for {filepath}")
        arma = arma_objs[0]
        filename = Path(filepath).stem

        do_fix_tail = get_do_fix_tail(bpy.context) #True
        if do_fix_tail: #!
            #ROTATE BONES
            fix_tail_started = time.perf_counter()
            bpy.context.view_layer.objects.active = None
            bpy.ops.object.select_all(action='DESELECT')
            bpy.context.view_layer.objects.active = arma
            arma.select_set(True)
            bpy.ops.object.mode_set(mode='EDIT')
            rotate_and_connect_bones(arma)
            bpy.ops.object.mode_set(mode='OBJECT')
            fix_tail_seconds = time.perf_counter() - fix_tail_started
            if fix_tail_seconds >= _REDCLOTH_PROFILE_WARN_THRESHOLD:
                _log_redcloth_profile_warning(
                    "fix tail %s %.3fs",
                    filename,
                    fix_tail_seconds,
                )

        collision_proxy_objects = {
            "spheres": None,
            "connections": None,
            "capsules": None,
        }

        if DO_WEAR_CLOTH:
            collision_started = time.perf_counter()
            cloth_group = createEmpty(filename,"_grp")
            collision_transform = createEmpty(filename, "Collision Spheres", cloth_group)
            connections_transform = createEmpty(filename, "Collision Connections", cloth_group)
            proxy_transform = createEmpty(filename, "Collision Proxies", cloth_group)
            arma.parent = cloth_group

            arma.name = filename
            arma.data.name = filename+"_ARM"
            arma.select_set(True)
            bpy.context.view_layer.objects.active = arma

            spheres_coll = bpy.data.collections.get("Collision Spheres")
            connect_coll = bpy.data.collections.get("Collision Connections")
            capsules_coll = bpy.data.collections.get("Collision Capsules")

            all_spheres_coll = list(spheres_coll.all_objects) if spheres_coll else []
            all_connect_coll = list(connect_coll.all_objects) if connect_coll else []
            all_capsules_coll = list(capsules_coll.all_objects) if capsules_coll else []
            if spheres_coll:
                _link_objects_to_collection(active_coll, all_spheres_coll)
                _unlink_objects_from_collection(spheres_coll, all_spheres_coll)
                _remove_collection_if_exists(spheres_coll)
            if connect_coll:
                _link_objects_to_collection(active_coll, all_connect_coll)
                _unlink_objects_from_collection(connect_coll, all_connect_coll)
                _remove_collection_if_exists(connect_coll)
            if capsules_coll:
                _link_objects_to_collection(active_coll, all_capsules_coll)
                _unlink_objects_from_collection(capsules_coll, all_capsules_coll)
                _remove_collection_if_exists(capsules_coll)

            _fix_connection_objects_transform_space(all_connect_coll)
            _parent_and_namespace_collision_objects(filename, collision_transform, all_spheres_coll, keep_transform=False)
            _parent_and_namespace_collision_objects(filename, connections_transform, all_connect_coll, keep_transform=False)
            if all_capsules_coll:
                capsules_transform = createEmpty(filename, "Collision Capsules", cloth_group)
                _parent_and_namespace_collision_objects(filename, capsules_transform, all_capsules_coll, keep_transform=False)

            collision_proxy_objects["spheres"] = _create_collision_proxy_object(
                _namespaced_name(filename, "Collision Spheres Proxy"),
                proxy_transform,
                active_coll,
                all_spheres_coll,
            )
            collision_proxy_objects["connections"] = _create_collision_proxy_object(
                _namespaced_name(filename, "Collision Connections Proxy"),
                proxy_transform,
                active_coll,
                all_connect_coll,
            )
            collision_proxy_objects["capsules"] = _create_collision_proxy_object(
                _namespaced_name(filename, "Collision Capsules Proxy"),
                proxy_transform,
                active_coll,
                all_capsules_coll,
            )
            proxy_count = sum(1 for proxy in collision_proxy_objects.values() if proxy is not None)
            collision_seconds = time.perf_counter() - collision_started
            if collision_seconds >= _REDCLOTH_PROFILE_WARN_THRESHOLD:
                _log_redcloth_profile_warning(
                    "collision setup %s %.3fs (spheres %d, connections %d, capsules %d, proxies %d)",
                    filename,
                    collision_seconds,
                    len(all_spheres_coll),
                    len(all_connect_coll),
                    len(all_capsules_coll),
                    proxy_count,
                )


        bpy.context.view_layer.objects.active = None
        bpy.ops.object.select_all(action='DESELECT')
        mesh_scan_started = time.perf_counter()
        GMesh_objs = []
        for ob in active_coll.all_objects:
            if ob.type == "MESH" and ob.name.startswith("GMesh_lod"):
                GMesh_objs.append(ob)
        GMesh_objs.sort(key=lambda x: x.name, reverse=False)
        gmesh_count = len(GMesh_objs)
        mesh_scan_seconds = time.perf_counter() - mesh_scan_started
        if not GMesh_objs:
            raise RuntimeError(f"No GMesh_lod mesh found after APX import for {filepath}")
        gmesh = GMesh_objs[0]
        if DO_WEAR_CLOTH:
            gmesh.name = filename+":"+gmesh.name
        mesh_name_payload = json.dumps([gmesh.name])
        try:
            arma["witcher_redcloth_mesh_name"] = gmesh.name
            arma["witcher_redcloth_mesh_names"] = mesh_name_payload
            if DO_WEAR_CLOTH and 'cloth_group' in locals():
                cloth_group["witcher_redcloth_mesh_name"] = gmesh.name
                cloth_group["witcher_redcloth_mesh_names"] = mesh_name_payload
        except Exception:
            pass

        for o in reversed(GMesh_objs):
            if "lod1" in o.name or \
                "lod2" in o.name or \
                "lod3" in o.name or \
                "lod4" in o.name:
                bpy.data.objects.remove(o)

        gmesh.select_set(True)
        bpy.context.view_layer.objects.active = gmesh

        material_stats = apply_redcloth_materials_to_meshes(
            [gmesh],
            filepath,
            mat_filename,
            context=context,
        )
        material_read_seconds = float(material_stats.get("read_seconds", 0.0) or 0.0)
        material_apply_seconds = float(material_stats.get("apply_seconds", 0.0) or 0.0)
        material_slot_count = int(material_stats.get("material_count", 0) or 0)
        if material_read_seconds >= _REDCLOTH_PROFILE_WARN_THRESHOLD or material_apply_seconds >= _REDCLOTH_PROFILE_WARN_THRESHOLD:
            _log_redcloth_profile_warning(
                "materials %s %.3fs (read %.3fs, apply %.3fs, slots %d)",
                gmesh.name,
                material_read_seconds + material_apply_seconds,
                material_read_seconds,
                material_apply_seconds,
                material_slot_count,
            )

        runtime_defaults_started = time.perf_counter()
        _apply_redcloth_runtime_defaults(gmesh, context)
        runtime_defaults_seconds = time.perf_counter() - runtime_defaults_started

        if DO_WEAR_CLOTH:
            patch_started = time.perf_counter()
            patched_collision_mode = _patch_clothsim_nodegroup_to_object_proxies(
                gmesh,
                collision_proxy_objects,
            )
            patch_seconds = time.perf_counter() - patch_started
            if not patched_collision_mode:
                log.warning(
                    "Redcloth import: could not patch ClothSimulation to object proxies for %s. "
                    "Collision may remain static or disabled without APX collections.",
                    gmesh.name,
                )
            if patch_seconds >= _REDCLOTH_PROFILE_WARN_THRESHOLD:
                _log_redcloth_profile_warning(
                    "patch proxies %s %.3fs (patched %s)",
                    gmesh.name,
                    patch_seconds,
                    "yes" if patched_collision_mode else "no",
                )

            if 'MaximumDistance' in gmesh.data.color_attributes:
                weights_started = time.perf_counter()
                vcol = gmesh.data.color_attributes['MaximumDistance']
                vgroup_id = 'SimplyPin'
                vgroup = gmesh.vertex_groups.new(name=vgroup_id)
                gmesh.vertex_groups.active_index = vgroup.index

                color_to_weights(gmesh, vcol, 0, vgroup.index)
                weights_seconds = time.perf_counter() - weights_started
                if weights_seconds >= _REDCLOTH_PROFILE_WARN_THRESHOLD:
                    _log_redcloth_profile_warning(
                        "pin weights %s %.3fs",
                        gmesh.name,
                        weights_seconds,
                    )

            try:
                merge_started = time.perf_counter()
                _merge_mesh_by_distance_data(gmesh, merge_threshold=0.0001)
                merge_seconds = time.perf_counter() - merge_started
                if merge_seconds >= _REDCLOTH_PROFILE_WARN_THRESHOLD:
                    _log_redcloth_profile_warning(
                        "merge distance %s %.3fs",
                        gmesh.name,
                        merge_seconds,
                    )
            except Exception as e:
                merge_seconds = time.perf_counter() - merge_started
                log.warning("Redcloth import: merge-by-distance failed for %s: %s", gmesh.name, e)

            # Move imported APX objects back into the collection the user started from.
            move_started = time.perf_counter()
            move_objects_between_collections(active_coll, save_collection)
            move_seconds = time.perf_counter() - move_started
            if move_seconds >= _REDCLOTH_PROFILE_WARN_THRESHOLD:
                _log_redcloth_profile_warning(
                    "move collections %s %.3fs",
                    filename,
                    move_seconds,
                )

        restore_started = time.perf_counter()
        _restore_import_context()
        restore_seconds = time.perf_counter() - restore_started

        total_seconds = time.perf_counter() - total_started
        if total_seconds >= _REDCLOTH_PROFILE_WARN_THRESHOLD:
            _log_redcloth_profile_warning(
                "total %s %.3fs (sanitize %.3fs, addon %.3fs, armatures %.3fs, fix_tail %.3fs, collision %.3fs, meshes %.3fs, material_read %.3fs, material_apply %.3fs, defaults %.3fs, patch %.3fs, weights %.3fs, merge %.3fs, move %.3fs, restore %.3fs, gmeshes %d, proxies %d, wear %s)",
                filename,
                total_seconds,
                sanitize_seconds,
                addon_import_seconds,
                armature_scan_seconds,
                fix_tail_seconds,
                collision_seconds,
                mesh_scan_seconds,
                material_read_seconds,
                material_apply_seconds,
                runtime_defaults_seconds,
                patch_seconds,
                weights_seconds,
                merge_seconds,
                move_seconds,
                restore_seconds,
                gmesh_count,
                proxy_count,
                "yes" if DO_WEAR_CLOTH else "no",
            )

        if DO_WEAR_CLOTH:
            return cloth_group
        else:
            return arma
    except Exception as e:
        total_seconds = time.perf_counter() - total_started
        if total_seconds >= _REDCLOTH_PROFILE_WARN_THRESHOLD:
            _log_redcloth_profile_warning(
                "failed %s %.3fs (sanitize %.3fs, addon %.3fs, armatures %.3fs, fix_tail %.3fs, collision %.3fs, meshes %.3fs, material_read %.3fs, material_apply %.3fs, defaults %.3fs, patch %.3fs, weights %.3fs, merge %.3fs, move %.3fs, restore %.3fs, gmeshes %d, proxies %d, wear %s, error %s)",
                os.path.basename(filepath),
                total_seconds,
                sanitize_seconds,
                addon_import_seconds,
                armature_scan_seconds,
                fix_tail_seconds,
                collision_seconds,
                mesh_scan_seconds,
                material_read_seconds,
                material_apply_seconds,
                runtime_defaults_seconds,
                patch_seconds,
                weights_seconds,
                merge_seconds,
                move_seconds,
                restore_seconds,
                gmesh_count,
                proxy_count,
                "yes" if DO_WEAR_CLOTH else "no",
                e,
            )
        log.warning("Redcloth import failed for %s: %s", os.path.basename(filepath), e)
        log.debug("Redcloth import traceback for %s", filepath, exc_info=True)
        try:
            _cleanup_failed_cloth_import(import_snapshot)
        except Exception as cleanup_exc:
            log.debug("Failed cleaning up partial cloth import for %s: %s", filepath, cleanup_exc)
        restore_started = time.perf_counter()
        _restore_import_context()
        restore_seconds = time.perf_counter() - restore_started
        return None
