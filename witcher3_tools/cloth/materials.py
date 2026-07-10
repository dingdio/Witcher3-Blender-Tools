"""Material loading and assignment for imported REDcloth meshes."""

import time
from pathlib import Path

import bpy

from .. import CR2W, get_texture_path
from ..materials.cr2w import (
    find_matching_material_on_object,
    load_w3_materials_CR2W,
)
from .geometry_nodes import apply_redcloth_runtime_defaults


__all__ = ["apply_redcloth_materials_to_meshes"]


def _redcloth_material_name_prefix(
    redcloth_resource: str = "",
    fallback_path: str = "",
) -> str:
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
            materials_handle = chunk.GetVariableByName("materials")
            if materials_handle and hasattr(materials_handle, "Handles"):
                materials = [redcloth_material[o.Reference] for o in materials_handle.Handles]
            apex_names = chunk.GetVariableByName("apexMaterialNames")
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
    """Apply CR2W material slots to APX mesh objects and return timing stats."""
    mesh_list = [
        obj
        for obj in (mesh_objects or [])
        if obj is not None and getattr(obj, "type", None) == "MESH"
    ]
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
    redcloth_material, materials, material_names, read_seconds = (
        _read_redcloth_material_payload(redcloth_resource, mat_filename)
    )
    result["read_seconds"] = read_seconds
    result["material_count"] = len(material_names)

    if not redcloth_material or not materials or not material_names:
        if apply_runtime_defaults:
            for mesh_obj in mesh_list:
                apply_redcloth_runtime_defaults(mesh_obj, ctx)
        return result

    total_apply_seconds = 0.0
    for mesh_obj in mesh_list:
        target_mat = False
        for idx, _material in enumerate(materials):
            if idx >= len(material_names):
                break
            target_mat = find_matching_material_on_object(mesh_obj, material_names[idx])
            if target_mat:
                break

        if not target_mat:
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
            apply_redcloth_runtime_defaults(mesh_obj, ctx)

    result["apply_seconds"] = total_apply_seconds
    return result
