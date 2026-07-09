from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger(__name__)


def _embedded_chunk(meshFile, reference) -> Optional[Any]:
    try:
        chunks = meshFile.CHUNKS.CHUNKS
        return chunks[reference]
    except Exception:
        return None


def _embedded_material_props(meshFile, reference, slot_name: str, warnings: list[str]) -> dict[str, Any]:
    chunk = _embedded_chunk(meshFile, reference)
    if chunk is None:
        warnings.append(f"{slot_name}: embedded material chunk {reference} missing; using fallback master")
        return {"base_custom": "", "local": True, "input_props": []}
    try:
        from ..materials.material import xml_data_from_CR2W

        xml = xml_data_from_CR2W(chunk, slot_name)
    except Exception as exc:
        warnings.append(f"{slot_name}: could not read embedded material ({exc}); using fallback master")
        return {"base_custom": "", "local": True, "input_props": []}

    input_props = [
        {"name": p.get("name"), "type": p.get("type"), "value": p.get("value")}
        for p in xml.findall("param")
    ]
    return {"base_custom": xml.get("base") or "", "local": True, "input_props": input_props}


def material_slots_from_mesh(
    the_material_names,
    the_materials,
    meshFile,
    version: int,
    warnings: list[str],
) -> list[dict[str, Any]]:
    handles = getattr(the_materials, "Handles", None) or []
    names = list(the_material_names or [])
    material_version = "witcher2" if int(version or 0) <= 115 else ""

    slots: list[dict[str, Any]] = []
    for index, handle in enumerate(handles):
        slot_name = names[index] if index < len(names) else f"Material{index}"
        reference = getattr(handle, "Reference", None)

        if reference is not None:
            props = _embedded_material_props(meshFile, reference, slot_name, warnings)
        else:
            depot = str(getattr(handle, "DepotPath", "") or "")
            if not depot:
                warnings.append(f"{slot_name}: material handle has no depot path; using fallback master")
            props = {"base_custom": depot, "local": False, "input_props": []}

        props["name"] = slot_name
        props["material_version"] = material_version
        slots.append({
            "name": slot_name,
            "material_slot_index": index,
            "witcher_props": props,
        })
    return slots
