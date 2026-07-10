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
from contextlib import contextmanager, nullcontext
from typing import Any, Optional

from .manifest import (
    OVERWRITE_CATEGORIES,
    build_manifest,
    default_content_root,
    depot_asset_dir,
    depot_asset_rel,
    normalize_content_root,
    normalize_overwrite,
    normalize_source_game,
    relpath_for_manifest,
    safe_asset_name,
)
from .export_armature import (
    _combined_export_armature_for_mesh_groups,
    _export_armature_for_mesh_group,
    _find_attachment_armature_for_missing_bones,  # re-exported for tests
    _group_armature,
    _hard_attachment_socket,
    _required_source_bone_names,  # re-exported for tests
    _resolve_export_armature,  # re-exported for tests
    _retargeted_armature_modifiers,
    _rigid_attachment_skinning,
    _special_attachment_socket,
)
from .material_chain import ChainBuilder
from .scene_utils import (
    _iter_bpy_objects,
    _remove_object,
    _restore_object_state,
    _select_only,
    _snapshot_object_state,
    _unique_temp_object_name,
)
from .texture_export import TextureRegistry

_LOD_SUFFIX_RE = re.compile(r"lod[\s_]?(\d+)", re.IGNORECASE)
_AUTO_CONTENT_ROOTS = {
    normalize_content_root("/Game/ImportedFbx").lower(),
    default_content_root("w2").lower(),
    default_content_root("w3").lower(),
}
_WOMAN_BASE_ASSET_REL = "characters/base_entities/woman_base/woman_base"
_MAN_BASE_ASSET_REL = "characters/base_entities/man_base/man_base"
_RETARGET_PREVIEW_FORCE_FULL_SOURCE_BONES = {
    "dyng_hair_a_null",
    "dyng_hair_b_null",
    "dyng_hair_c_null",
    "dyng_hair_d_null",
    "dyng_hair_e_null",
    "dyng_hair_x_null",
    "iris",
    "placer_iris",
}
_RETARGET_PREVIEW_EXCLUDED_BONES = {
    "dyng_backbag_01",
    "dyng_dagger_01",
    "dyng_frontbag_01",
    "steel_sword_scabbard",
    "steel_sword_scabbard_1",
    "steel_sword_scabbard_2",
    "steel_sword_scabbard_3",
}


def _preview_mesh_asset_rel_for_blueprint(blueprint_asset_rel: str) -> str:
    blueprint_asset_rel = str(blueprint_asset_rel or "").strip().replace("\\", "/")
    folder, _, name = blueprint_asset_rel.rpartition("/")
    preview_name = f"SKM_{safe_asset_name(name or blueprint_asset_rel)}_RetargetPreview"
    return f"{folder}/{preview_name}" if folder else preview_name


def default_export_folder() -> str:
    try:
        from ..extension_paths import get_temp_root

        return os.path.join(get_temp_root(), "unreal_exports")
    except Exception:
        return os.path.join(os.getcwd(), "witcher_unreal_exports")


def overwrite_policy_from_settings(settings) -> dict[str, bool]:
    return normalize_overwrite(
        {key: bool(getattr(settings, f"overwrite_{key}", False)) for key in OVERWRITE_CATEGORIES}
    )


def _resolve_content_root_setting(content_root: str, source_game: str) -> str:
    raw = str(content_root or "").strip()
    if not raw:
        return default_content_root(source_game)
    normalized = normalize_content_root(raw, source_game)
    if normalized.lower() in _AUTO_CONTENT_ROOTS:
        return default_content_root(source_game)
    return normalized


def _infer_export_source_game(export_objects, main_armature=None) -> tuple[str, list[str]]:
    sources: list[tuple[str, str]] = []

    def add(obj):
        raw = _object_source_game(obj)
        if raw:
            sources.append((normalize_source_game(raw), str(getattr(obj, "name", "") or "object")))

    add(main_armature)
    for obj in export_objects or []:
        if obj is main_armature:
            continue
        add(obj)
        if getattr(obj, "type", "") == "MESH":
            add(getattr(obj, "parent", None))
            for modifier in getattr(obj, "modifiers", []) or []:
                if getattr(modifier, "type", "") == "ARMATURE":
                    add(getattr(modifier, "object", None))

    if not sources:
        return "w3", []

    primary = sources[0][0]
    found = {game for game, _label in sources}
    if len(found) <= 1:
        return primary, []

    labels = ", ".join(f"{label}={game}" for game, label in sources[:4])
    return primary, [
        f"Mixed W2/W3 source metadata in selection; using {primary.upper()} content root ({labels})."
    ]


def _object_source_game(obj) -> str:
    if obj is None:
        return ""
    for key in ("witcher_source_game", "source_game"):
        try:
            value = obj.get(key, "")
        except Exception:
            value = ""
        if str(value or "").strip():
            return str(value)

    rig_settings = getattr(getattr(obj, "data", None), "witcherui_RigSettings", None)
    value = getattr(rig_settings, "source_game", "") if rig_settings is not None else ""
    return str(value or "").strip()


def _refresh_view_layer(context) -> None:
    try:
        view_layer = getattr(context, "view_layer", None)
        if view_layer is not None:
            view_layer.update()
    except Exception:
        pass


def build_unreal_export_bundle(context, settings) -> dict[str, Any]:
    selected_objects = list(getattr(context, "selected_objects", []) or [])
    preload_warnings = _ensure_selected_character_appearances_loaded(context, selected_objects)
    _refresh_view_layer(context)
    export_objects = collect_export_objects(selected_objects)
    mesh_objects = [obj for obj in export_objects if getattr(obj, "type", "") == "MESH"]
    armatures = [obj for obj in export_objects if getattr(obj, "type", "") == "ARMATURE"]
    if not mesh_objects and not armatures:
        raise ValueError("Select at least one mesh or armature.")

    main_armature = _select_main_armature(armatures)
    asset_name = safe_asset_name(getattr(settings, "asset_name", "") or _guess_asset_name(main_armature, selected_objects, mesh_objects, armatures))
    export_root = str(getattr(settings, "export_folder", "") or default_export_folder())
    bundle_root = os.path.join(export_root, asset_name)
    os.makedirs(bundle_root, exist_ok=True)

    registry = TextureRegistry(bundle_root, parallel=True)
    chain = ChainBuilder(registry.register)

    source_game, source_warnings = _infer_export_source_game(export_objects, main_armature)
    content_root = _resolve_content_root_setting(getattr(settings, "content_root", ""), source_game)
    warnings: list[str] = list(preload_warnings) + source_warnings

    mesh_entries: list[dict[str, Any]] = []
    used_fbx_stems: dict[str, str] = {}
    use_buffers = bool(getattr(settings, "prefer_source_buffers", True))
    groups = group_meshes_by_depot(mesh_objects, asset_name, warnings)
    for group in groups:
        mesh_entries.append(
            _export_mesh_group(context, group, bundle_root, chain, warnings, used_fbx_stems,
                               main_armature=main_armature, use_buffers=use_buffers)
        )

    rig_entry = None
    if main_armature is not None:
        rig_entry = _export_rig(context, main_armature, bundle_root, warnings, used_fbx_stems)

    animation_entries = _export_animations(context, main_armature, bundle_root, asset_name, warnings)
    blueprint_entry = _build_blueprint_entry(
        main_armature, asset_name, mesh_entries, rig_entry, animation_entries
    )
    if blueprint_entry is not None:
        _export_retarget_preview_fbx(
            context,
            groups,
            main_armature,
            blueprint_entry,
            bundle_root,
            used_fbx_stems,
            warnings,
        )
    retarget_setup = _build_retarget_setup(rig_entry, blueprint_entry)

    manifest = build_manifest(
        asset_name=asset_name,
        bundle_root=bundle_root,
        source_game=source_game,
        content_root=content_root,
        overwrite=overwrite_policy_from_settings(settings),
        meshes=mesh_entries,
        masters=chain.ordered_masters(),
        materials=chain.ordered_materials(),
        textures=registry.manifest_entries(),
        animations=animation_entries,
        rig=rig_entry,
        blueprint=blueprint_entry,
        retarget_setup=retarget_setup,
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

def _ensure_selected_character_appearances_loaded(context, selected_objects) -> list[str]:
    warnings: list[str] = []
    for armature in _iter_selected_character_armatures(selected_objects):
        if list(_iter_current_character_export_objects(armature)):
            continue
        warning = _load_current_character_appearance_for_export(context, armature)
        if warning:
            warnings.append(warning)
    return warnings


def _iter_selected_character_armatures(selected_objects):
    seen = set()

    def add(candidate):
        if not _is_character_armature(candidate):
            return
        key = id(candidate)
        if key in seen:
            return
        seen.add(key)
        yield candidate

    for obj in selected_objects or []:
        yield from add(obj)
        if getattr(obj, "type", "") == "MESH":
            parent = getattr(obj, "parent", None)
            yield from add(parent)
            for modifier in getattr(obj, "modifiers", []) or []:
                if getattr(modifier, "type", "") == "ARMATURE":
                    yield from add(getattr(modifier, "object", None))


def _load_current_character_appearance_for_export(context, armature) -> str:
    rig_settings = getattr(getattr(armature, "data", None), "witcherui_RigSettings", None)
    app_list = getattr(rig_settings, "app_list", None) if rig_settings is not None else None
    try:
        app_idx = int(getattr(rig_settings, "app_list_index", -1))
    except Exception:
        app_idx = -1
    if app_list is None or app_idx < 0 or app_idx >= len(app_list):
        return ""

    item = app_list[app_idx]
    app_name = str(getattr(item, "name", "") or "").strip()
    if not app_name:
        return ""

    try:
        import_entity = _import_addon_module(".importers.import_entity")
    except Exception as exc:
        return f"{getattr(armature, 'name', 'Character')}: could not load current appearance '{app_name}' before Unreal export: {exc}"

    try:
        common_blender = _import_addon_module(".CR2W.common_blender", prefer_root=_module_root(import_entity))
        mod_loading_context = getattr(common_blender, "mod_loading_context", None)
    except Exception:
        mod_loading_context = None

    saved_state = _snapshot_object_state(context)
    try:
        _select_only(context, armature)
        loading_context = mod_loading_context(context) if callable(mod_loading_context) else nullcontext()
        with loading_context:
            import_entity.import_from_list_item(context, item)
    except Exception as exc:
        return f"{getattr(armature, 'name', 'Character')}: failed to load current appearance '{app_name}' before Unreal export: {exc}"
    finally:
        _restore_object_state(context, saved_state)
    return ""


def _module_root(module) -> str:
    package = str(getattr(module, "__package__", "") or getattr(module, "__name__", "") or "")
    for marker in (".importers", ".CR2W", ".ui", ".unreal_export"):
        marker_index = package.find(marker)
        if marker_index > 0:
            return package[:marker_index]
    return package.rsplit(".", 1)[0] if "." in package else package


def _import_addon_module(suffix: str, prefer_root: str = ""):
    import importlib
    import sys

    suffix = str(suffix or "")
    if not suffix.startswith("."):
        suffix = "." + suffix
    preferred_names = []
    if prefer_root:
        preferred_names.append(prefer_root + suffix)
    current_root = (__package__ or "").split(".unreal_export", 1)[0]
    if current_root:
        preferred_names.append(current_root + suffix)

    loaded = [
        (name, module)
        for name, module in sys.modules.items()
        if name.endswith(suffix) and module is not None
    ]
    loaded.sort(key=lambda item: 0 if item[0].startswith("bl_ext.") else 1)
    for name, module in loaded:
        if prefer_root and not name.startswith(prefer_root + "."):
            continue
        return module
    for name, module in loaded:
        return module
    for name in preferred_names:
        return importlib.import_module(name)
    raise ImportError(f"Could not resolve add-on module '{suffix}'")


def collect_export_objects(selected_objects) -> list[Any]:
    objects = []
    seen = set()

    def add(obj):
        name = str(getattr(obj, "name_full", "") or getattr(obj, "name", "") or "")
        if obj is None or not name or name in seen:
            return
        if getattr(obj, "type", "") not in {"MESH", "ARMATURE", "EMPTY"}:
            return
        if getattr(obj, "type", "") == "MESH" and _is_character_proxy_mesh(obj):
            return
        seen.add(name)
        objects.append(obj)

    def add_hierarchy(
        root,
        *,
        visible_only: bool = False,
        visible_children_only: bool = False,
        character_mesh_filter: bool = False,
    ):
        if visible_only and not _object_exports_for_current_view(root):
            return
        add(root)
        if getattr(root, "type", "") in {"ARMATURE", "EMPTY"}:
            for child in getattr(root, "children_recursive", []) or getattr(root, "children", []):
                if getattr(child, "type", "") in {"MESH", "EMPTY"}:
                    if (visible_only or visible_children_only) and not _object_exports_for_current_view(child):
                        continue
                    if (
                        character_mesh_filter
                        and getattr(child, "type", "") == "MESH"
                        and not _is_character_export_mesh(child)
                    ):
                        continue
                    add(child)

    for obj in selected_objects:
        add_hierarchy(
            obj,
            visible_children_only=True,
            character_mesh_filter=_is_character_armature(obj),
        )
        if getattr(obj, "type", "") == "MESH":
            parent = getattr(obj, "parent", None)
            if parent and getattr(parent, "type", "") == "ARMATURE":
                add(parent)
            for modifier in getattr(obj, "modifiers", []) or []:
                arm_obj = getattr(modifier, "object", None)
                if getattr(modifier, "type", "") == "ARMATURE" and arm_obj:
                    add(arm_obj)

    for armature in [obj for obj in list(objects) if getattr(obj, "type", "") == "ARMATURE"]:
        for related in _iter_current_character_export_objects(armature):
            add(related)

    return objects


def _iter_current_character_export_objects(armature):
    rig_settings = getattr(getattr(armature, "data", None), "witcherui_RigSettings", None)
    if rig_settings is None:
        return []

    try:
        ui_equipment = _import_addon_module(".ui.ui_equipment")
        find_objects_by_guid = ui_equipment.find_objects_by_guid
    except Exception:
        return []

    related = []
    seen = set()

    def add_guid_objects(guid: str, prop_name: str):
        guid = str(guid or "").strip()
        if not guid:
            return
        for obj in find_objects_by_guid(guid, prop_name):
            for mesh_obj in _iter_visible_character_export_meshes(obj):
                obj_id = id(mesh_obj)
                if obj_id in seen:
                    continue
                seen.add(obj_id)
                related.append(mesh_obj)

    for slot in getattr(rig_settings, "template_slots", []) or []:
        if not bool(getattr(slot, "is_loaded", False)):
            continue
        add_guid_objects(getattr(slot, "template_guid", ""), "witcher_template_guid")

    for slot in getattr(rig_settings, "equipment_slots", []) or []:
        if not bool(getattr(slot, "is_loaded", False)):
            continue
        add_guid_objects(getattr(slot, "equip_guid", ""), "witcher_equip_guid")

    return related


def _iter_visible_character_export_meshes(root):
    if not _object_exports_for_current_view(root):
        return
    if getattr(root, "type", "") == "MESH":
        if _is_character_export_mesh(root):
            yield root
        return
    for child in getattr(root, "children_recursive", []) or getattr(root, "children", []):
        if getattr(child, "type", "") != "MESH":
            continue
        if not _object_exports_for_current_view(child):
            continue
        if not _is_character_export_mesh(child):
            continue
        yield child


def _is_character_export_mesh(obj) -> bool:
    if getattr(obj, "type", "") != "MESH":
        return False
    if _is_character_proxy_mesh(obj):
        return False
    depot = mesh_depot_path(obj)
    if not depot:
        return False
    lowered = depot_asset_rel(depot).lower()
    if "shadowmesh" in lowered or "shadowmesh" in str(getattr(obj, "name", "")).lower():
        return False
    return True


def _is_character_proxy_mesh(obj) -> bool:
    name = str(getattr(obj, "name", "") or "").lower()
    data_name = str(getattr(getattr(obj, "data", None), "name", "") or "").lower()
    depot = depot_asset_rel(mesh_depot_path(obj)).lower()
    text = " ".join((name, data_name, depot))
    return (
        "_px" in text
        or ":collision" in text
        or "collision proxy" in text
        or "cloth" in text and "_px" in name
    )


def _is_character_armature(obj) -> bool:
    if getattr(obj, "type", "") != "ARMATURE":
        return False
    return getattr(getattr(obj, "data", None), "witcherui_RigSettings", None) is not None


def _object_exports_for_current_view(obj) -> bool:
    if obj is None:
        return False
    try:
        if obj.hide_get():
            return False
    except Exception:
        pass
    if bool(getattr(obj, "hide_viewport", False)) or bool(getattr(obj, "hide_render", False)):
        return False
    try:
        visible_get = getattr(obj, "visible_get", None)
        if callable(visible_get) and not visible_get():
            return False
    except Exception:
        pass
    return True


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


# ---- mesh / rig export ----

def _unique_bundle_file(bundle_root: str, asset_rel: str, used_stems: dict[str, str],
                        subdir: str, ext: str) -> str:
    base = asset_rel.rsplit("/", 1)[-1]
    stem = base
    counter = 2
    while used_stems.get(stem, asset_rel) != asset_rel:
        stem = f"{base}_{counter}"
        counter += 1
    used_stems[stem] = asset_rel

    out_dir = os.path.join(bundle_root, subdir)
    _makedirs_safe(out_dir)
    return os.path.join(out_dir, f"{stem}{ext}")


def _unique_fbx_path(bundle_root: str, asset_rel: str, used_stems: dict[str, str],
                     subdir: str = "Meshes") -> str:
    return _unique_bundle_file(bundle_root, asset_rel, used_stems, subdir, ".fbx")


def _stored_mesh_signature(obj) -> str:
    try:
        return str(obj.witcherui_MeshSettings.get("source_signature", "") or "")
    except Exception:
        return ""


def _object_has_morphs(obj) -> bool:
    keys = getattr(getattr(obj, "data", None), "shape_keys", None)
    blocks = getattr(keys, "key_blocks", None) if keys else None
    return bool(blocks) and len(blocks) > 1


def _group_is_unedited(objs) -> bool:
    from .mesh_signature import mesh_geometry_signature

    for obj in objs:
        stored = _stored_mesh_signature(obj)
        if not stored or stored != mesh_geometry_signature(obj):
            return False
    return True


def _resolve_source_w2mesh(depot: str) -> str:
    if not depot:
        return ""
    try:
        from ..CR2W.common_blender import repo_file

        abs_path = repo_file(depot)
    except Exception:
        return ""
    return abs_path if abs_path and os.path.isfile(abs_path) else ""


def _try_gather_group_buffer(context, group, main_armature, bundle_root: str,
                             used_stems: dict[str, str], warnings: list[str]):
    objs = group["objects"]
    if not objs or _hard_attachment_socket(objs, main_armature):
        return None
    if any(_object_has_morphs(obj) for obj in objs):
        return None
    source = _resolve_source_w2mesh(mesh_depot_path(objs[0]))
    if not source or not _group_is_unedited(objs):
        return None

    from . import gather as gather_mod
    from .mesh_buffer import write_mesh_buffer

    asset_rel = group["asset_path"]
    group_armature = _group_armature(objs)

    export_skeleton = None
    try:
        if group_armature is not None or main_armature is not None:
            with _export_armature_for_mesh_group(
                context, group_armature, main_armature, asset_rel, warnings
            ) as export_arm:
                if export_arm is not None:
                    export_skeleton = gather_mod.extract_armature_skeleton(export_arm)
    except Exception as exc:
        warnings.append(f"{asset_rel}: skeleton extract failed ({exc}); using FBX")
        return None

    try:
        mesh = gather_mod.gather_mesh(source, keep_lod_meshes=False, export_skeleton=export_skeleton)
    except Exception as exc:
        warnings.append(f"{asset_rel}: buffer gather failed ({exc}); using FBX")
        return None
    if not mesh.submeshes:
        return None
    if mesh.is_skinned and export_skeleton is None:
        return None  # no armature to merge -> FBX is the safe path
    if mesh.unresolved_bones:
        warnings.append(
            f"{asset_rel}: {len(mesh.unresolved_bones)} skin bone(s) not on the export "
            f"skeleton, weighted to root (e.g. {mesh.unresolved_bones[0]})"
        )

    buf_path = _unique_bundle_file(bundle_root, asset_rel, used_stems, "Meshes", ".w3buf")
    write_mesh_buffer(buf_path, mesh)
    return relpath_for_manifest(buf_path, bundle_root), mesh.is_skinned


def _export_mesh_group(context, group, bundle_root: str, chain: ChainBuilder, warnings: list[str],
                       used_fbx_stems: dict[str, str], main_armature=None,
                       use_buffers: bool = False) -> dict[str, Any]:
    asset_rel = group["asset_path"]
    asset_dir = depot_asset_dir(asset_rel)
    mesh_name = asset_rel.rsplit("/", 1)[-1]

    group_armature = _group_armature(group["objects"])

    attach_to_bone = (
        _special_attachment_socket(group_armature)
        if group_armature is not None and group_armature is not main_armature
        else None
    )

    buffer_rel = None
    if use_buffers and not attach_to_bone:
        gathered = _try_gather_group_buffer(context, group, main_armature, bundle_root, used_fbx_stems, warnings)
        if gathered is not None:
            buffer_rel, buffer_skinned = gathered

    if buffer_rel is not None:
        armature = None
    elif attach_to_bone:
        fbx_path = _unique_fbx_path(bundle_root, asset_rel, used_fbx_stems)
        armature = group_armature
        export_fbx(context, list(group["objects"]) + [armature], fbx_path)
        warnings.append(
            f"{asset_rel}: exported on its own skeleton, attached to rig bone "
            f"'{attach_to_bone}' in the blueprint"
        )
    else:
        fbx_path = _unique_fbx_path(bundle_root, asset_rel, used_fbx_stems)
        slot_bone = (
            _hard_attachment_socket(group["objects"], main_armature)
            if group_armature is None
            else None
        )
        attachment_ctx = (
            _rigid_attachment_skinning(
                context, group["objects"], main_armature, slot_bone, asset_rel, warnings
            )
            if slot_bone
            else nullcontext()
        )
        with attachment_ctx:
            if slot_bone:
                group_armature = _group_armature(group["objects"])
            with _export_armature_for_mesh_group(
                context, group_armature, main_armature, asset_rel, warnings
            ) as armature:
                fbx_objects = list(group["objects"]) + ([armature] if armature else [])
                retarget = (
                    _retargeted_armature_modifiers(group["objects"], armature)
                    if armature is not None and armature is not group_armature
                    else nullcontext()
                )
                with retarget:
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

    entry = {
        "name": mesh_name,
        "asset_path": asset_rel,
        "kind": "skeletal" if (buffer_skinned if buffer_rel is not None else armature) else "static",
        "slots": slots,
    }
    if buffer_rel is not None:
        entry["buffer"] = buffer_rel
    else:
        entry["fbx"] = relpath_for_manifest(fbx_path, bundle_root)
    if attach_to_bone:
        entry["own_skeleton"] = True
        entry["attach_to_bone"] = attach_to_bone
    return entry


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


def _scale_temp_armature_data(armature, scale: float) -> None:
    if armature is None or abs(float(scale) - 1.0) < 0.000001:
        return
    from mathutils import Matrix

    armature.data.transform(Matrix.Scale(float(scale), 4))


@contextmanager
def _scaled_mesh_object_copies(context, mesh_objects, scale: float):
    if abs(float(scale) - 1.0) < 0.000001:
        yield list(mesh_objects or [])
        return

    import bpy
    from mathutils import Matrix

    collection = getattr(context, "collection", None)
    if collection is None:
        collection = getattr(getattr(context, "scene", None), "collection", None)
    if collection is None:
        collection = bpy.context.scene.collection

    scale_matrix = Matrix.Scale(float(scale), 4)
    copies = []
    try:
        for obj in mesh_objects or []:
            dup = obj.copy()
            dup.name = _unique_temp_object_name("__witcher_unreal_export_preview_mesh", _iter_bpy_objects())
            dup.data = obj.data.copy()
            dup.data.name = _unique_temp_object_name("__witcher_unreal_export_preview_mesh_data", bpy.data.meshes)
            dup.data.transform(scale_matrix)
            try:
                dup.data.update()
            except Exception:
                pass
            try:
                matrix = obj.matrix_world.copy()
                matrix.translation = matrix.translation * float(scale)
                dup.matrix_world = matrix
            except Exception:
                pass
            collection.objects.link(dup)
            copies.append(dup)
        yield copies
    finally:
        for obj in copies:
            _remove_object(obj)


def _export_retarget_preview_fbx(context, groups, main_armature, blueprint_entry: dict[str, Any],
                                 bundle_root: str, used_fbx_stems: dict[str, str],
                                 warnings: list[str]) -> None:
    if main_armature is None:
        return

    mesh_asset_paths = {
        str(path or "").strip().replace("\\", "/")
        for path in blueprint_entry.get("mesh_asset_paths", []) or []
        if str(path or "").strip()
    }
    if not mesh_asset_paths:
        return

    preview_groups = []
    preview_mesh_objects = []
    for group in groups or []:
        if str(group.get("asset_path", "") or "").replace("\\", "/") not in mesh_asset_paths:
            continue
        group_armature = _group_armature(group.get("objects", []))
        if _special_attachment_socket(group_armature):
            continue
        if _hard_attachment_socket(group.get("objects", []), main_armature):
            continue
        preview_groups.append(group)
        preview_mesh_objects.extend(group.get("objects", []) or [])

    unique_preview_mesh_objects = []
    seen_mesh_ids: set[int] = set()
    for obj in preview_mesh_objects:
        if getattr(obj, "type", "") != "MESH":
            continue
        obj_id = id(obj)
        if obj_id in seen_mesh_ids:
            continue
        seen_mesh_ids.add(obj_id)
        unique_preview_mesh_objects.append(obj)
    preview_mesh_objects = unique_preview_mesh_objects
    if not preview_mesh_objects:
        return

    preview_asset_rel = _preview_mesh_asset_rel_for_blueprint(str(blueprint_entry.get("asset_path", "") or ""))
    fbx_path = _unique_fbx_path(bundle_root, preview_asset_rel, used_fbx_stems)
    group_armatures = [_group_armature(group.get("objects", [])) for group in preview_groups]
    with _combined_export_armature_for_mesh_groups(
        context,
        group_armatures,
        main_armature,
        preview_asset_rel,
        warnings,
        force_full_source_bone_names=_RETARGET_PREVIEW_FORCE_FULL_SOURCE_BONES,
        exclude_bone_names=_RETARGET_PREVIEW_EXCLUDED_BONES,
    ) as preview_armature:
        if preview_armature is None:
            return
        _scale_temp_armature_data(preview_armature, 100.0)
        with _retargeted_armature_modifiers(preview_mesh_objects, preview_armature):
            with _scaled_mesh_object_copies(context, preview_mesh_objects, 100.0) as scaled_preview_mesh_objects:
                export_fbx(
                    context,
                    scaled_preview_mesh_objects + [preview_armature],
                    fbx_path,
                    apply_scale_options="FBX_SCALE_UNITS",
                )

    blueprint_entry["retarget_preview_asset_path"] = preview_asset_rel
    blueprint_entry["retarget_preview_fbx"] = relpath_for_manifest(fbx_path, bundle_root)
    warnings.append(
        f"{preview_asset_rel}: exported combined retarget preview FBX with "
        f"{len(preview_mesh_objects)} mesh object(s)"
    )


def _build_blueprint_entry(armature, asset_name: str, mesh_entries,
                           rig_entry=None, animation_entries=None) -> Optional[dict[str, Any]]:
    skeletal_entries = [entry for entry in mesh_entries if entry.get("kind") == "skeletal"]
    follower_paths = [
        entry["asset_path"] for entry in skeletal_entries if not entry.get("attach_to_bone")
    ]
    attachments = [
        {"asset_path": entry["asset_path"], "attach_to_bone": entry["attach_to_bone"]}
        for entry in skeletal_entries
        if entry.get("attach_to_bone")
    ]
    skeletal_paths = follower_paths
    base_mesh_asset_path = str((rig_entry or {}).get("asset_path", "") or "")
    if armature is None or (not skeletal_paths and not attachments):
        return None
    if not base_mesh_asset_path and len(skeletal_paths) < 2 and not attachments:
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

    entry = {
        "name": name,
        "asset_path": asset_rel,
        "mesh_asset_paths": skeletal_paths,
    }
    if attachments:
        entry["attachments"] = attachments
    if base_mesh_asset_path:
        entry["base_mesh_asset_path"] = base_mesh_asset_path
    if animation_entries:
        # The blueprint's base component plays this clip on spawn, mirroring
        # the animation currently applied to the armature in Blender.
        entry["animation_asset_path"] = str(animation_entries[0].get("asset_path", "") or "")
    return entry


def _build_retarget_setup(rig_entry=None, blueprint_entry=None) -> Optional[dict[str, Any]]:
    rig_asset = str((rig_entry or {}).get("asset_path", "") or "").strip().replace("\\", "/").lower()
    base_asset = str((blueprint_entry or {}).get("base_mesh_asset_path", "") or "").strip().replace("\\", "/").lower()
    if rig_asset == _WOMAN_BASE_ASSET_REL or base_asset == _WOMAN_BASE_ASSET_REL:
        target_profile = "woman_base"
        target_asset = _WOMAN_BASE_ASSET_REL
    elif rig_asset == _MAN_BASE_ASSET_REL or base_asset == _MAN_BASE_ASSET_REL:
        target_profile = "man_base"
        target_asset = _MAN_BASE_ASSET_REL
    else:
        return None
    return {
        "target_profile": target_profile,
        "source_profile": "mannequin",
        "target_mesh_asset_path": target_asset,
        "target_skeleton_asset_path": target_asset + "_Skeleton",
        "source_mesh_asset_path": "/Game/Characters/Mannequins/Meshes/SKM_Quinn_Simple",
        "source_mesh_fallback_asset_path": "/Game/Characters/Mannequins/Meshes/SKM_Manny_Simple",
        "output_folder": "/Game/RETARGET_",
    }


# ---- animation export ----

def _enabled_animation_entries(context) -> list[Any]:
    scene = getattr(context, "scene", None)
    export_set = getattr(scene, "witcher_anim_export_set", None) if scene else None
    if not export_set:
        return []
    return [entry for entry in export_set if bool(getattr(entry, "enabled", True))]


def _repo_path_from_abs(file_path: str) -> str:
    try:
        from ..importers.import_mesh import get_repo_from_abs_path

        repo_rel = get_repo_from_abs_path(os.path.normpath(file_path))
    except Exception:
        return ""
    if not repo_rel or os.path.isabs(repo_rel) or os.path.splitdrive(repo_rel)[0]:
        return ""
    return repo_rel


def _normalize_animset_depot_path(path: str) -> str:
    raw = str(path or "").strip().strip('"')
    if not raw:
        return ""
    if os.path.isabs(raw) or os.path.splitdrive(raw)[0]:
        raw = _repo_path_from_abs(raw)
    raw = raw.replace("/", "\\").lstrip("\\")
    lowered = raw.lower()
    if lowered.endswith(".w2anims.json"):
        raw = raw[:-5]
        lowered = raw.lower()
    if not lowered.endswith(".w2anims"):
        return ""
    return raw


def _action_source_animset_depot(action, context=None) -> str:
    candidates = []
    try:
        candidates.append(action.get("w3_anim_source_file", ""))
    except Exception:
        pass
    scene = getattr(context, "scene", None) if context else None
    if scene is not None:
        candidates.append(getattr(scene, "witcher_loaded_w2anims_path", ""))
        candidates.append(getattr(scene, "witcher_anim_export_repo_path", ""))
    for candidate in candidates:
        depot = _normalize_animset_depot_path(candidate)
        if depot:
            return depot
    return ""


def _unique_animation_asset_rel(source_animset: str, action_name: str, asset_name: str,
                                used_asset_paths: set[str]) -> str:
    clip_name = safe_asset_name(action_name, "Animation")
    animset_rel = depot_asset_rel(source_animset)
    if animset_rel:
        base_rel = f"{animset_rel}/{clip_name}"
    else:
        base_rel = f"custom/{safe_asset_name(asset_name)}/animations/{clip_name}"

    asset_rel = base_rel
    counter = 2
    while asset_rel in used_asset_paths:
        asset_rel = f"{base_rel}_{counter}"
        counter += 1
    used_asset_paths.add(asset_rel)
    return asset_rel


def _action_frame_range(action) -> tuple[int, int]:
    try:
        start, end = action.frame_range
    except Exception:
        return 1, 1
    start = int(round(float(start)))
    end = int(round(float(end)))
    if end < start:
        end = start
    return start, end


def _current_armature_action(armature, frame=None):
    """The action the armature is actually playing: the assigned action, or
    the NLA strip under the playhead. Clips loaded from the addon's anim list
    play from NLA strips (the 'anim_import' track), so checking
    ``animation_data.action`` alone misses them."""
    anim_data = getattr(armature, "animation_data", None)
    if anim_data is None:
        return None
    action = getattr(anim_data, "action", None)
    if action is not None:
        return action

    tracks = [t for t in (getattr(anim_data, "nla_tracks", []) or []) if not getattr(t, "mute", False)]
    solo = [t for t in tracks if getattr(t, "is_solo", False)]
    if solo:
        tracks = solo
    tracks.sort(key=lambda t: 0 if getattr(t, "name", "") == "anim_import" else 1)

    fallback = None
    for track in tracks:
        for strip in getattr(track, "strips", []) or []:
            strip_action = getattr(strip, "action", None)
            if strip_action is None or getattr(strip, "mute", False):
                continue
            if (frame is not None
                    and getattr(strip, "frame_start", None) is not None
                    and strip.frame_start <= frame <= strip.frame_end):
                return strip_action
            if fallback is None:
                fallback = strip_action
    return fallback


def _collect_export_actions(context, armature, warnings: list[str]) -> list[Any]:
    """Enabled Export Set actions, or the action currently playing on the
    armature when the Export Set is empty (the "send what I see" path)."""
    entries = _enabled_animation_entries(context)
    if entries:
        import bpy

        actions = []
        for entry in entries:
            action_name = str(getattr(entry, "action_name", "") or "").strip()
            if not action_name:
                warnings.append("Animation export set entry has no action name; skipped.")
                continue
            action = bpy.data.actions.get(action_name)
            if action is None:
                warnings.append(f"Animation export set entry '{action_name}' was not found; skipped.")
                continue
            actions.append(action)
        return actions

    scene = getattr(context, "scene", None)
    frame = getattr(scene, "frame_current", None) if scene else None
    action = _current_armature_action(armature, frame)
    return [action] if action is not None else []


def _export_animations(context, armature, bundle_root: str, asset_name: str,
                       warnings: list[str]) -> list[dict[str, Any]]:
    if armature is None:
        if _enabled_animation_entries(context):
            warnings.append("Animation export set is enabled, but no armature was selected.")
        return []

    actions = _collect_export_actions(context, armature, warnings)
    if not actions:
        return []

    animation_entries: list[dict[str, Any]] = []
    used_asset_paths: set[str] = set()
    used_fbx_stems: dict[str, str] = {}
    for action in actions:
        source_animset = _action_source_animset_depot(action, context)
        asset_rel = _unique_animation_asset_rel(source_animset, action.name, asset_name, used_asset_paths)
        fbx_path = _unique_fbx_path(bundle_root, asset_rel, used_fbx_stems, subdir="Animations")
        export_animation_fbx(context, armature, action, fbx_path)
        frame_start, frame_end = _action_frame_range(action)
        manifest_entry = {
            "name": asset_rel.rsplit("/", 1)[-1],
            "action_name": action.name,
            "fbx": relpath_for_manifest(fbx_path, bundle_root),
            "asset_path": asset_rel,
            "frame_start": frame_start,
            "frame_end": frame_end,
        }
        if source_animset:
            manifest_entry["source_animset"] = source_animset
        else:
            warnings.append(
                f"{action.name}: no .w2anims source path found; exporting to '{asset_rel}'"
            )
        animation_entries.append(manifest_entry)

    return animation_entries


def export_animation_fbx(context, armature, action, fbx_path: str) -> None:
    from ..animation.action_compat import resolve_action_slot

    scene = context.scene
    anim_data = armature.animation_data_create()
    original_action = getattr(anim_data, "action", None)
    original_action_slot = getattr(anim_data, "action_slot", None) if hasattr(anim_data, "action_slot") else None
    original_use_nla = getattr(anim_data, "use_nla", None) if hasattr(anim_data, "use_nla") else None
    original_frame_start = getattr(scene, "frame_start", None)
    original_frame_end = getattr(scene, "frame_end", None)
    original_frame_current = getattr(scene, "frame_current", None)
    frame_start, frame_end = _action_frame_range(action)

    try:
        if original_use_nla is not None:
            anim_data.use_nla = False
        anim_data.action = action
        if hasattr(anim_data, "action_slot"):
            action_slot = resolve_action_slot(action, target=armature, ensure=True)
            if action_slot is not None:
                anim_data.action_slot = action_slot
        scene.frame_start = frame_start
        scene.frame_end = frame_end
        try:
            scene.frame_set(frame_start)
        except Exception:
            pass
        export_fbx(
            context,
            [armature],
            fbx_path,
            object_types={"ARMATURE"},
            bake_anim=True,
        )
    finally:
        anim_data.action = original_action
        if original_use_nla is not None:
            anim_data.use_nla = original_use_nla
        if hasattr(anim_data, "action_slot"):
            try:
                anim_data.action_slot = original_action_slot
            except Exception:
                pass
        if original_frame_start is not None:
            scene.frame_start = original_frame_start
        if original_frame_end is not None:
            scene.frame_end = original_frame_end
        if original_frame_current is not None:
            try:
                scene.frame_set(original_frame_current)
            except Exception:
                pass


def export_fbx(context, export_objects, fbx_path: str, *, object_types=None,
               bake_anim: bool = False, apply_scale_options: str = "FBX_SCALE_NONE",
               global_scale: float = 1.0) -> None:
    import bpy

    if object_types is None:
        object_types = {"MESH", "ARMATURE", "EMPTY"}
    saved_state = _snapshot_object_state(context)
    try:
        if saved_state[2] != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        bpy.ops.object.select_all(action="DESELECT")
        for obj in export_objects:
            obj.select_set(True)
        context.view_layer.objects.active = next((obj for obj in export_objects if obj.type == "MESH"), export_objects[0])
        with _unreal_armature_export_names(export_objects):
            bpy.ops.export_scene.fbx(
                filepath=fbx_path,
                use_selection=True,
                global_scale=global_scale,
                object_types=object_types,
                add_leaf_bones=False,
                armature_nodetype="NULL",
                # Keep the full armature in every skeletal FBX. Stripping
                # non-deform bones can produce different parent chains per mesh,
                # which UE cannot merge into one shared skeleton.
                use_armature_deform_only=False,
                # Unreal warns if Blender emits no FBX smoothing-group data.
                mesh_smooth_type="FACE",
                # Write per-loop tangents + binormals so UE imports the exact
                # tangent basis (NormalImportMethod=ImportNormalsAndTangents) rather
                # than recomputing it and seaming at vertex splits. Tangents are
                # emitted for the active UV layer, so every exported mesh needs UVs.
                use_tspace=True,
                bake_anim=bake_anim,
                bake_anim_use_all_bones=True,
                bake_anim_use_nla_strips=False,
                bake_anim_use_all_actions=False,
                bake_anim_force_startend_keying=True,
                bake_anim_step=1.0,
                bake_anim_simplify_factor=0.0,
                axis_forward="-Z",
                axis_up="Y",
                apply_unit_scale=True,
                # Keep Blender's m->cm factor on the Armature root for UE import.
                apply_scale_options=apply_scale_options,
                bake_space_transform=False,
                path_mode="AUTO",
            )
    finally:
        _restore_object_state(context, saved_state)


@contextmanager
def _unreal_armature_export_names(export_objects):
    armature = next((obj for obj in export_objects if getattr(obj, "type", "") == "ARMATURE"), None)
    if armature is None:
        yield
        return

    bpy_objects = _iter_bpy_objects()
    renamed = []
    try:
        if getattr(armature, "name", "") != "Armature":
            all_objects = list(bpy_objects) + list(export_objects)
            for obj in bpy_objects:
                if obj is armature or getattr(obj, "name", "") != "Armature":
                    continue
                temp_name = _unique_temp_object_name("__witcher_unreal_export_armature", all_objects)
                renamed.append((obj, obj.name))
                obj.name = temp_name
                all_objects.append(obj)

            renamed.append((armature, armature.name))
            armature.name = "Armature"
        yield
    finally:
        for obj, name in reversed(renamed):
            try:
                obj.name = name
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


def _is_special_attachment_armature(obj) -> bool:
    try:
        return bool(obj.get("w2_special_attachment"))
    except Exception:
        return False


def _armature_entity_depot(obj) -> str:
    rig_settings = getattr(getattr(obj, "data", None), "witcherui_RigSettings", None)
    return str(getattr(rig_settings, "repo_path", "") or "").strip()


def _select_main_armature(armatures):
    if not armatures:
        return None
    candidates = [a for a in armatures if not _is_special_attachment_armature(a)] or list(armatures)
    for arm in candidates:
        if _armature_entity_depot(arm).lower().endswith((".w2ent", ".w3ent")):
            return arm
    return candidates[0]


def _guess_asset_name(main_armature, selected_objects, mesh_objects, armatures) -> str:
    if main_armature is not None:
        rig_settings = getattr(getattr(main_armature, "data", None), "witcherui_RigSettings", None)
        entity_name = str(getattr(rig_settings, "entity_name", "") or "").strip()
        return entity_name or main_armature.name
    for obj in selected_objects:
        if getattr(obj, "type", "") == "ARMATURE":
            return obj.name
    if armatures:
        return armatures[0].name
    if mesh_objects:
        return mesh_objects[0].name
    return "WitcherAsset"
