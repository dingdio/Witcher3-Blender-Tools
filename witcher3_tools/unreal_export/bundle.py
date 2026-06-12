"""Blender-side Unreal export bundle builder.

Builds a depot-mirrored bundle: one FBX per source ``.w2mesh`` (named after
the mesh so the Unreal asset gets the depot name), textures and material
chains at their depot paths, an optional rig FBX for the ``.w2rig`` skeleton,
and an optional blueprint section for ``.w2ent`` character templates.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

from .manifest import (
    build_manifest,
    depot_asset_dir,
    depot_asset_rel,
    relpath_for_manifest,
    safe_asset_name,
)
from .material_chain import ChainBuilder
from .texture_export import TextureRegistry

_LOD_SUFFIX_RE = re.compile(r"lod[\s_]?(\d+)", re.IGNORECASE)


def default_export_folder() -> str:
    try:
        from ..extension_paths import get_temp_root

        return os.path.join(get_temp_root(), "unreal_exports")
    except Exception:
        return os.path.join(os.getcwd(), "witcher_unreal_exports")


def build_unreal_export_bundle(context, settings) -> dict[str, Any]:
    selected_objects = list(getattr(context, "selected_objects", []) or [])
    export_objects = collect_export_objects(selected_objects)
    mesh_objects = [obj for obj in export_objects if getattr(obj, "type", "") == "MESH"]
    armatures = [obj for obj in export_objects if getattr(obj, "type", "") == "ARMATURE"]
    if not mesh_objects and not armatures:
        raise ValueError("Select at least one mesh or armature.")

    asset_name = safe_asset_name(getattr(settings, "asset_name", "") or _guess_asset_name(selected_objects, mesh_objects, armatures))
    export_root = str(getattr(settings, "export_folder", "") or default_export_folder())
    content_root = str(getattr(settings, "content_root", "") or "")
    bundle_root = os.path.join(export_root, asset_name)
    os.makedirs(bundle_root, exist_ok=True)

    registry = TextureRegistry(bundle_root)
    chain = ChainBuilder(registry.register)
    warnings: list[str] = []

    mesh_entries: list[dict[str, Any]] = []
    used_fbx_stems: dict[str, str] = {}
    groups = group_meshes_by_depot(mesh_objects, asset_name, warnings)
    for group in groups:
        mesh_entries.append(
            _export_mesh_group(context, group, bundle_root, chain, warnings, used_fbx_stems)
        )

    rig_entry = None
    main_armature = armatures[0] if armatures else None
    if main_armature is not None:
        rig_entry = _export_rig(context, main_armature, bundle_root, warnings, used_fbx_stems)

    blueprint_entry = _build_blueprint_entry(main_armature, asset_name, mesh_entries)

    manifest = build_manifest(
        asset_name=asset_name,
        bundle_root=bundle_root,
        content_root=content_root,
        meshes=mesh_entries,
        masters=chain.ordered_masters(),
        materials=chain.ordered_materials(),
        textures=registry.manifest_entries(),
        rig=rig_entry,
        blueprint=blueprint_entry,
        warnings=warnings + chain.warnings + registry.warnings,
    )

    manifest_path = os.path.join(bundle_root, "witcher_unreal_export.json")
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)

    return {
        "asset_name": asset_name,
        "bundle_root": bundle_root,
        "manifest_path": manifest_path,
        "manifest": manifest,
    }


# ---- object collection / grouping ----

def collect_export_objects(selected_objects) -> list[Any]:
    objects = []
    seen = set()

    def add(obj):
        if obj is None or obj.name_full in seen:
            return
        if getattr(obj, "type", "") not in {"MESH", "ARMATURE", "EMPTY"}:
            return
        seen.add(obj.name_full)
        objects.append(obj)

    for obj in selected_objects:
        add(obj)
        if getattr(obj, "type", "") == "ARMATURE":
            for child in getattr(obj, "children_recursive", []) or getattr(obj, "children", []):
                if getattr(child, "type", "") in {"MESH", "EMPTY"}:
                    add(child)
        if getattr(obj, "type", "") == "MESH":
            parent = getattr(obj, "parent", None)
            if parent and getattr(parent, "type", "") == "ARMATURE":
                add(parent)
            for modifier in getattr(obj, "modifiers", []) or []:
                arm_obj = getattr(modifier, "object", None)
                if getattr(modifier, "type", "") == "ARMATURE" and arm_obj:
                    add(arm_obj)

    return objects


def mesh_depot_path(mesh_obj) -> str:
    settings = getattr(mesh_obj, "witcherui_MeshSettings", None)
    return str(getattr(settings, "item_repo_path", "") or "").strip()


def _object_lod_index(obj) -> int:
    match = None
    for source in (getattr(obj, "name", ""), getattr(getattr(obj, "data", None), "name", "")):
        for found in _LOD_SUFFIX_RE.finditer(str(source or "")):
            match = found
    return int(match.group(1)) if match else 0


def group_meshes_by_depot(mesh_objects, asset_name: str, warnings: list[str]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for obj in mesh_objects:
        depot = mesh_depot_path(obj)
        if depot:
            asset_rel = depot_asset_rel(depot)
        else:
            asset_rel = f"custom/{safe_asset_name(asset_name)}/{safe_asset_name(obj.name)}"
            warnings.append(
                f"{obj.name}: no Witcher repo path on the mesh; exporting to '{asset_rel}'"
            )
        group = groups.setdefault(asset_rel, {"asset_path": asset_rel, "objects": []})
        group["objects"].append(obj)

    for group in groups.values():
        lod_levels = {_object_lod_index(obj) for obj in group["objects"]}
        if len(lod_levels) > 1:
            min_lod = min(lod_levels)
            group["objects"] = [obj for obj in group["objects"] if _object_lod_index(obj) == min_lod]
    return list(groups.values())


def _group_armature(group_objects) -> Optional[Any]:
    for obj in group_objects:
        for modifier in getattr(obj, "modifiers", []) or []:
            if getattr(modifier, "type", "") == "ARMATURE" and getattr(modifier, "object", None):
                return modifier.object
        parent = getattr(obj, "parent", None)
        if parent is not None and getattr(parent, "type", "") == "ARMATURE":
            return parent
    return None


# ---- mesh / rig export ----

def _unique_fbx_path(bundle_root: str, asset_rel: str, used_stems: dict[str, str]) -> str:
    """Flat FBX filenames: mirroring depot dirs on disk broke Windows MAX_PATH
    inside Blender. The Unreal asset name/path comes from the manifest, not
    from the FBX location."""
    base = asset_rel.rsplit("/", 1)[-1]
    stem = base
    counter = 2
    while used_stems.get(stem, asset_rel) != asset_rel:
        stem = f"{base}_{counter}"
        counter += 1
    used_stems[stem] = asset_rel

    fbx_dir = os.path.join(bundle_root, "Meshes")
    _makedirs_safe(fbx_dir)
    return os.path.join(fbx_dir, f"{stem}.fbx")


def _export_mesh_group(context, group, bundle_root: str, chain: ChainBuilder, warnings: list[str],
                       used_fbx_stems: dict[str, str]) -> dict[str, Any]:
    asset_rel = group["asset_path"]
    asset_dir = depot_asset_dir(asset_rel)
    mesh_name = asset_rel.rsplit("/", 1)[-1]

    fbx_path = _unique_fbx_path(bundle_root, asset_rel, used_fbx_stems)

    armature = _group_armature(group["objects"])
    fbx_objects = list(group["objects"]) + ([armature] if armature else [])
    export_fbx(context, fbx_objects, fbx_path)

    slots: list[dict[str, Any]] = []
    seen_slots: set[tuple[int, str]] = set()
    for obj in group["objects"]:
        for mat_info in collect_material_infos(obj, warnings):
            slot_index = int(mat_info.get("material_slot_index", len(slots)))
            slot_name = str(mat_info.get("name", ""))
            key = (slot_index, slot_name)
            if key in seen_slots:
                continue
            seen_slots.add(key)
            material_id = chain.add_slot_material(mat_info, asset_dir)
            slots.append({
                "slot_index": slot_index,
                "slot_name": slot_name,
                "material_id": material_id,
            })

    return {
        "name": mesh_name,
        "fbx": relpath_for_manifest(fbx_path, bundle_root),
        "asset_path": asset_rel,
        "kind": "skeletal" if armature else "static",
        "slots": slots,
    }


def _armature_rig_depot(armature) -> str:
    rig_settings = getattr(getattr(armature, "data", None), "witcherui_RigSettings", None)
    skeleton = str(getattr(rig_settings, "main_entity_skeleton", "") or "").strip()
    if not skeleton:
        return ""
    if os.path.isabs(skeleton) or os.path.splitdrive(skeleton)[0]:
        try:
            from ..importers.import_mesh import get_repo_from_abs_path

            skeleton = get_repo_from_abs_path(os.path.normpath(skeleton)) or ""
        except Exception:
            return ""
    if not skeleton.lower().endswith((".w2rig", ".w3dyng")):
        return ""
    return skeleton


def _export_rig(context, armature, bundle_root: str, warnings: list[str],
                used_fbx_stems: dict[str, str]) -> Optional[dict[str, Any]]:
    rig_depot = _armature_rig_depot(armature)
    if not rig_depot:
        return None

    asset_rel = depot_asset_rel(rig_depot)
    rig_name = asset_rel.rsplit("/", 1)[-1]

    fbx_path = _unique_fbx_path(bundle_root, asset_rel, used_fbx_stems)

    dummy = _create_rig_dummy_mesh(context, armature)
    try:
        export_fbx(context, [armature, dummy], fbx_path)
    finally:
        _remove_object(dummy)

    return {
        "name": rig_name,
        "fbx": relpath_for_manifest(fbx_path, bundle_root),
        "asset_path": asset_rel,
    }


def _create_rig_dummy_mesh(context, armature):
    """UE refuses skeleton-only FBX imports, so skin a tiny triangle to the root bone."""
    import bpy

    size = 0.01
    mesh = bpy.data.meshes.new("witcher_rig_dummy")
    mesh.from_pydata([(0.0, 0.0, 0.0), (size, 0.0, 0.0), (0.0, size, 0.0)], [], [(0, 1, 2)])
    mesh.update()

    obj = bpy.data.objects.new("witcher_rig_dummy", mesh)
    context.scene.collection.objects.link(obj)
    obj.parent = armature

    root_bone = next((bone.name for bone in armature.data.bones if bone.parent is None), None)
    if root_bone:
        group = obj.vertex_groups.new(name=root_bone)
        group.add([0, 1, 2], 1.0, "REPLACE")
    modifier = obj.modifiers.new("Armature", "ARMATURE")
    modifier.object = armature
    return obj


def _remove_object(obj) -> None:
    import bpy

    try:
        mesh = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        if mesh and mesh.users == 0:
            bpy.data.meshes.remove(mesh)
    except Exception:
        pass


def _build_blueprint_entry(armature, asset_name: str, mesh_entries) -> Optional[dict[str, Any]]:
    skeletal_paths = [entry["asset_path"] for entry in mesh_entries if entry.get("kind") == "skeletal"]
    if armature is None or len(skeletal_paths) < 2:
        return None

    rig_settings = getattr(getattr(armature, "data", None), "witcherui_RigSettings", None)
    entity_depot = str(getattr(rig_settings, "repo_path", "") or "").strip()
    entity_name = str(getattr(rig_settings, "entity_name", "") or "").strip()

    if entity_depot.lower().endswith((".w2ent", ".w3ent")):
        asset_rel = depot_asset_rel(entity_depot)
        name = asset_rel.rsplit("/", 1)[-1]
    else:
        name = safe_asset_name(entity_name or asset_name)
        asset_rel = f"custom/{safe_asset_name(asset_name)}/BP_{name}"

    return {
        "name": name,
        "asset_path": asset_rel,
        "mesh_asset_paths": skeletal_paths,
    }


def export_fbx(context, export_objects, fbx_path: str) -> None:
    import bpy

    original_active = context.view_layer.objects.active
    original_selection = list(context.selected_objects)
    original_mode = context.object.mode if context.object else "OBJECT"
    try:
        if original_mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        bpy.ops.object.select_all(action="DESELECT")
        for obj in export_objects:
            obj.select_set(True)
        context.view_layer.objects.active = next((obj for obj in export_objects if obj.type == "MESH"), export_objects[0])
        bpy.ops.export_scene.fbx(
            filepath=fbx_path,
            use_selection=True,
            object_types={"MESH", "ARMATURE", "EMPTY"},
            add_leaf_bones=False,
            use_armature_deform_only=True,
            axis_forward="-Z",
            axis_up="Y",
            apply_unit_scale=True,
            bake_space_transform=False,
            path_mode="AUTO",
        )
    finally:
        bpy.ops.object.select_all(action="DESELECT")
        for obj in original_selection:
            if obj and obj.name in bpy.data.objects:
                obj.select_set(True)
        if original_active and original_active.name in bpy.data.objects:
            context.view_layer.objects.active = original_active
        if original_mode != "OBJECT" and context.view_layer.objects.active:
            try:
                bpy.ops.object.mode_set(mode=original_mode)
            except Exception:
                pass


def collect_material_infos(mesh_obj, warnings: list[str]) -> list[dict[str, Any]]:
    from ..exporters.export_mesh import get_mesh_material_info

    mesh = getattr(mesh_obj, "data", None)
    if mesh is None:
        return []
    slot_indices = list(range(len(getattr(mesh, "materials", []) or [])))
    try:
        infos = get_mesh_material_info(mesh, mesh_obj=mesh_obj, material_slot_indices=slot_indices)
    except ValueError as exc:
        warnings.append(f"{mesh_obj.name}: material validation failed: {exc}")
        return []

    materials = list(getattr(mesh, "materials", []) or [])
    aligned: list[dict[str, Any]] = []
    for info in infos:
        info = dict(info)
        slot_index = int(info.get("material_slot_index", len(aligned)))
        props = dict(info.get("witcher_props") or {})
        mat = materials[slot_index] if 0 <= slot_index < len(materials) else None
        mat_props = getattr(mat, "witcher_props", None) if mat else None
        props.setdefault("material_version", str(getattr(mat_props, "material_version", "") or ""))
        info["witcher_props"] = props
        info["material_slot_index"] = slot_index
        aligned.append(info)
    return aligned


def _makedirs_safe(path: str) -> None:
    try:
        from ..CR2W.common_blender import win_safe_path

        os.makedirs(win_safe_path(path), exist_ok=True)
    except Exception:
        os.makedirs(path, exist_ok=True)


def _guess_asset_name(selected_objects, mesh_objects, armatures) -> str:
    for obj in selected_objects:
        if getattr(obj, "type", "") == "ARMATURE":
            return obj.name
    if armatures:
        return armatures[0].name
    if mesh_objects:
        return mesh_objects[0].name
    return "WitcherAsset"
