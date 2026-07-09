"""Build Blender materials from parsed CR2W material chunks."""

import logging
import re
from xml.dom import minidom
from xml.etree import ElementTree

import bpy
from bpy.types import Material, Object

from .material import setup_w3_material, xml_data_from_CR2W


log = logging.getLogger(__name__)


def prettify(elem) -> str:
    """Return a pretty-printed XML representation of an element."""
    rough_string = ElementTree.tostring(elem, "utf-8")
    try:
        reparsed = minidom.parseString(rough_string)
        return reparsed.toprettyxml(indent="\t")
    except Exception:
        log.warning(
            "Material XML prettify failed; storing compact XML instead.",
            exc_info=True,
        )
        return rough_string.decode("utf-8", errors="ignore")


def setup_w3_material_CR2W(
    uncook_path: str,
    bl_material: Material,
    mat_bin,
    force_update=False,
    mat_filename="",
    is_instance_file=False,
    build_nodes=True,
):
    new_xml = xml_data_from_CR2W(mat_bin, bl_material.name)
    bl_material.use_nodes = bool(build_nodes)

    bl_material.witcher_props.name = bl_material.name
    bl_material.witcher_props.base_custom = new_xml.get("base")
    bl_material.witcher_props.local = True
    bl_material.witcher_props.xml_text = prettify(new_xml)

    if (
        hasattr(mat_bin, "DepotPath")
        and hasattr(mat_bin, "local")
        and mat_bin.local == False
    ):
        bl_material.witcher_props.base_custom = mat_bin.DepotPath
        bl_material.witcher_props.local = False

    if mat_bin.get_CR2W_version() <= 115:
        bl_material.witcher_props.material_version = "witcher2"

    enable_mask = mat_bin.GetVariableByName("enableMask")
    if enable_mask and enable_mask.Value == 1:
        bl_material.witcher_props.enableMask = True
    if not build_nodes:
        return bl_material

    from .nodes import (
        auto_load_base_material_snapshot,
        refresh_witcher_include_state,
        suspend_witcher_include_updates,
    )

    finished_mat = setup_w3_material(
        uncook_path,
        bl_material,
        xml_data=new_xml,
        xml_path=mat_filename,
        force_update=force_update,
        is_instance_file=is_instance_file,
        defer_include_refresh=True,
    )
    try:
        with suspend_witcher_include_updates():
            auto_load_base_material_snapshot(
                bpy.context,
                finished_mat,
                create_missing=True,
            )
    except Exception:
        log.warning(
            "Failed to auto-load Base Path snapshot for material '%s'",
            getattr(finished_mat, "name", "<unknown>"),
            exc_info=True,
        )
    finally:
        refresh_witcher_include_state(finished_mat)
    return finished_mat


def _material_name_base(name: str) -> str:
    return re.sub(r"\.\d{3}$", "", str(name or ""))


def find_matching_material_on_object(obj: Object, material_name: str):
    if obj is None or getattr(obj, "type", None) != "MESH":
        return None

    try:
        if material_name in obj.data.materials:
            return obj.data.materials[material_name]
    except Exception:
        pass

    material_base = _material_name_base(material_name)
    for material in obj.data.materials:
        if material is None:
            continue
        candidate_base = _material_name_base(material.name)
        if candidate_base == material_base:
            log.info("Material base match %s -> %s", material_name, material.name)
            return material
        if material_base and (
            material_base in candidate_base or candidate_base in material_base
        ):
            log.info("Material partial match %s -> %s", material_name, material.name)
            return material
    return None


def load_w3_materials_CR2W(
    obj: Object,
    uncook_path: str,
    materials_bin,
    material_names,
    force_mat_update=False,
    mat_filename="",
):
    for index, material_chunk in enumerate(materials_bin):
        if material_chunk is None:
            slot_name = material_names[index] if index < len(material_names) else "?"
            log.warning(
                "Skipping unresolved material at slot %d (%s)",
                index,
                slot_name,
            )
            continue

        material_name = material_names[index]
        log.info(material_name)
        target_material = find_matching_material_on_object(obj, material_name)
        if not target_material:
            continue

        finished_material = setup_w3_material_CR2W(
            uncook_path,
            target_material,
            material_chunk,
            force_update=force_mat_update,
            mat_filename=mat_filename,
        )
        obj.material_slots[target_material.name].material = finished_material
