import logging
import hashlib
import json
import copy
from dataclasses import dataclass, field
from pathlib import Path
import re
from ..CR2W.CR2W_helpers import Enums
from ..CR2W.CR2W_types import is_entity_chunk
from ..CR2W.prop_utils import read_prop_value
import bpy
from bpy.app.handlers import persistent
import os
from ..importers.import_helpers import MatrixToArray, MeshReferenceMissing, checkLevel, meshPath, set_blender_object_transform, _transform_real
from ..importers.entity_light import configure_entity_light, orient_red_spot
from mathutils import Matrix, Euler
from math import radians
import time

log = logging.getLogger(__name__)

_MESH_IMPORT_TIMING_ENABLED = True
_MESH_IMPORT_WARN_THRESHOLD = 0.25
_LAYER_IMPORT_PROFILE_ENABLED = True
_LAYER_IMPORT_PROFILE_WARN_THRESHOLD = 0.25
CACHED_LAYER_TRANSFORM_MODE_VERSION = 11

from .. import fbx_util
from .. import get_uncook_path
from .. import get_W3_FOLIAGE_PATH
from .. import get_fbx_uncook_path
from .. import get_use_fbx_repo
from .. import get_do_import_redcloth
from ..importers import import_mesh, import_isolation
from ..external_addon_tools import get_srt_addon_status

from bpy_extras.wm_utils.progress_report import (
    ProgressReport,
    ProgressReportSubstep,
)


class lightObject:
    def __init__(self, meshName = "Light Item",
                    translation = False,
                    matrix = False,
                    transform = False,
                    block = False,
                    BlockDataObjectType = Enums.BlockDataObjectType.Mesh):
        self.name = meshName
        self.meshName = meshName
        self.translation = translation
        self.matrix = matrix
        self.transform = transform
        self.type = "Light"
        self.block = block
        self.BlockDataObjectType = BlockDataObjectType

from ..CR2W.common_blender import repo_file, win_safe_path
from ..repo_paths import resolve_w2_repo_file_from_root
# def repo_file(filepath: str):
#     if filepath.endswith('.fbx'):
#         return os.path.join(bpy.context.preferences.addons['io_import_w2l'].preferences.fbx_uncook_path, filepath)
#     else:
#         return os.path.join(bpy.context.preferences.addons['io_import_w2l'].preferences.uncook_path, filepath)

def _log_layer_import_start(level_file):
    log.info("Importing layer: %s", level_file)


def _log_layer_import_complete(level_file, progress_count, errors):
    if errors:
        log.error("Layer import finished with %d error(s): %s", len(errors), level_file)
        for error in errors:
            log.error(error)
        return
    if progress_count:
        log.info("Finished layer: %s", level_file)
    else:
        log.info("Layer contained no importable items: %s", level_file)


def _preview_list(items, limit=12):
    values = [str(item) for item in (items or []) if str(item)]
    if len(values) <= limit:
        return ", ".join(values)
    return ", ".join(values[:limit]) + f", ... (+{len(values) - limit} more)"


def _mesh_repo_path(mesh) -> str:
    return str(getattr(mesh, "meshName", "") or "").strip()


def _append_import_error(errors, message: str) -> None:
    if errors is None:
        return
    try:
        errors.append(message)
    except Exception:
        pass


def _normalize_embedded_cmesh_chunk_index(value):
    if value is None:
        return None
    if isinstance(value, (list, tuple, set)):
        values = sorted({int(index) for index in value if index is not None})
        if not values:
            return None
        if len(values) > 1:
            raise ValueError(
                f"embedded_cmesh_chunk_index selects one Witcher 2 CMesh chunk; got {values}"
            )
        return values[0]
    return int(value)


def _embedded_cmesh_chunk_index(mesh):
    value = getattr(mesh, "embedded_cmesh_chunk_index", None)
    if value is None:
        value = getattr(mesh, "mesh_chunk_indices", None)
    return _normalize_embedded_cmesh_chunk_index(value)


def _mesh_cr2w_version(mesh, fallback=999):
    try:
        return int(getattr(mesh, "cr2w_version", fallback) or fallback)
    except Exception:
        try:
            return int(fallback)
        except Exception:
            return 999


def _existing_mesh_path_from_explicit_root(mesh_name, mesh, version=999):
    mesh_name = str(mesh_name or "").replace("/", "\\").lstrip("\\")
    if not mesh_name or os.path.isabs(mesh_name):
        return ""
    root = str(getattr(mesh, "uncook_path", "") or "").strip()
    if not root:
        return ""
    candidate = os.path.join(root, mesh_name)
    if os.path.exists(win_safe_path(candidate)):
        return candidate
    try:
        if int(version) <= 115:
            return resolve_w2_repo_file_from_root(mesh_name, root)
    except Exception:
        pass
    return ""


def _mesh_scene_repo_key(mesh_name, embedded_cmesh_chunk_index=None):
    mesh_name = str(mesh_name or "").strip()
    embedded_cmesh_chunk_index = _normalize_embedded_cmesh_chunk_index(embedded_cmesh_chunk_index)
    if not mesh_name or embedded_cmesh_chunk_index is None:
        return mesh_name
    return f"{mesh_name}#cmesh={embedded_cmesh_chunk_index}"


def _layer_load_mode_signature(dev_empty_only=False):
    return f"dev_empty={int(bool(dev_empty_only))};transform={CACHED_LAYER_TRANSFORM_MODE_VERSION}"


def _set_layer_import_state(collection, level_file, state, progress_count=0, error_count=0, filtered_count=0, *, nearby_filter=None, mode_signature=None, plan_hash=None):
    if collection is None or not hasattr(collection, "__setitem__"):
        return
    try:
        collection["witcher_layer_import_state"] = str(state or "").strip().lower()
        collection["witcher_layer_import_level"] = str(level_file or "")
        collection["witcher_layer_import_count"] = int(progress_count or 0)
        collection["witcher_layer_import_errors"] = int(error_count or 0)
        collection["witcher_layer_import_filtered"] = int(filtered_count or 0)
    except Exception:
        pass
    if nearby_filter is not None:
        try:
            cam = nearby_filter.get("camera_position") or (0.0, 0.0, 0.0)
            collection["witcher_layer_load_camera_x"] = float(cam[0])
            collection["witcher_layer_load_camera_y"] = float(cam[1])
            collection["witcher_layer_load_camera_z"] = float(cam[2])
            collection["witcher_layer_load_radius"] = float(nearby_filter.get("radius", 0.0) or 0.0)
        except Exception:
            pass
    if mode_signature is not None:
        try:
            # Persist the mode even without a nearby filter.
            collection["witcher_layer_load_mode"] = str(mode_signature)
        except Exception:
            pass
    if plan_hash is not None:
        try:
            collection["witcher_layer_import_plan_hash"] = str(plan_hash or "")
        except Exception:
            pass


class LayerImportCancelled(RuntimeError):
    pass


def _layer_import_cancel_requested(kwargs):
    cancel_check = kwargs.get("_cancel_check")
    if not callable(cancel_check):
        return False
    try:
        return bool(cancel_check())
    except Exception:
        return False


def _raise_if_layer_import_cancelled(kwargs):
    if _layer_import_cancel_requested(kwargs):
        raise LayerImportCancelled("Cancelled by user")


def _log_mesh_import_timing_warning(message, *args):
    if not _MESH_IMPORT_TIMING_ENABLED:
        return
    log.info("[mesh-import-profile] " + str(message), *args)


def _log_layer_import_profile_warning(message, *args):
    if not _LAYER_IMPORT_PROFILE_ENABLED:
        return
    log.info("[layer-import-profile] " + str(message), *args)


def _new_layer_import_profile():
    return {
        "mesh_calls": 0,
        "mesh_total_seconds": 0.0,
        "mesh_import_seconds": 0.0,
        "mesh_finalize_seconds": 0.0,
        "mesh_transform_seconds": 0.0,
        "backend_counts": {},
        "reused_meshes": 0,
        "fresh_meshes": 0,
        "unique_mesh_paths": set(),
        "slowest_mesh": {"path": "", "seconds": 0.0, "backend": ""},
    }


def _get_layer_import_profile(kwargs):
    profile = kwargs.get("_layer_import_profile")
    if profile is None:
        profile = _new_layer_import_profile()
        kwargs["_layer_import_profile"] = profile
    return profile


def _record_layer_mesh_profile(
    kwargs,
    mesh,
    backend,
    reused_existing,
    total_seconds,
    import_seconds,
    finalize_seconds,
    transform_seconds,
):
    profile = _get_layer_import_profile(kwargs)
    profile["mesh_calls"] += 1
    profile["mesh_total_seconds"] += float(total_seconds or 0.0)
    profile["mesh_import_seconds"] += float(import_seconds or 0.0)
    profile["mesh_finalize_seconds"] += float(finalize_seconds or 0.0)
    profile["mesh_transform_seconds"] += float(transform_seconds or 0.0)
    mesh_name = str(getattr(mesh, "meshName", "") or "")
    if mesh_name:
        profile["unique_mesh_paths"].add(mesh_name)
    if reused_existing:
        profile["reused_meshes"] += 1
    else:
        profile["fresh_meshes"] += 1
    backend_entry = profile["backend_counts"].setdefault(
        str(backend or "unknown"),
        {"count": 0, "seconds": 0.0},
    )
    backend_entry["count"] += 1
    backend_entry["seconds"] += float(total_seconds or 0.0)
    if total_seconds >= profile["slowest_mesh"]["seconds"]:
        profile["slowest_mesh"] = {
            "path": mesh_name,
            "seconds": float(total_seconds or 0.0),
            "backend": str(backend or "unknown"),
        }


def _log_layer_import_profile_summary(level_file, kwargs):
    profile = kwargs.get("_layer_import_profile")
    if not profile:
        return
    mesh_total_seconds = float(profile.get("mesh_total_seconds", 0.0) or 0.0)
    if mesh_total_seconds < _LAYER_IMPORT_PROFILE_WARN_THRESHOLD:
        return

    backend_bits = []
    for backend_name, backend_entry in sorted(
        profile.get("backend_counts", {}).items(),
        key=lambda item: item[1].get("seconds", 0.0),
        reverse=True,
    ):
        backend_bits.append(
            f"{backend_name} {int(backend_entry.get('count', 0) or 0)}/{float(backend_entry.get('seconds', 0.0) or 0.0):.3f}s"
        )
    backend_summary = ", ".join(backend_bits) if backend_bits else "none"

    slowest_mesh = profile.get("slowest_mesh", {}) or {}
    _log_layer_import_profile_warning(
        "%s meshes %d total %.3fs (import %.3fs, finalize %.3fs, transform %.3fs, fresh %d, reused %d, unique %d, backends %s, slowest mesh %s %.3fs %s)",
        level_file,
        int(profile.get("mesh_calls", 0) or 0),
        mesh_total_seconds,
        float(profile.get("mesh_import_seconds", 0.0) or 0.0),
        float(profile.get("mesh_finalize_seconds", 0.0) or 0.0),
        float(profile.get("mesh_transform_seconds", 0.0) or 0.0),
        int(profile.get("fresh_meshes", 0) or 0),
        int(profile.get("reused_meshes", 0) or 0),
        len(profile.get("unique_mesh_paths", set()) or ()),
        backend_summary,
        slowest_mesh.get("path", "") or "<none>",
        float(slowest_mesh.get("seconds", 0.0) or 0.0),
        slowest_mesh.get("backend", "") or "",
    )


_LAYER_IMPORT_OWNER_PROP = "witcher_layer_owner"
_LAYER_IMPORT_PLAN_ITEM_PROP = "witcher_layer_plan_item_id"
_LAYER_IMPORT_PLAN_MODE_PROP = "witcher_layer_plan_mode"
_CACHED_REDCLOTH_ITEM_KINDS = frozenset({"cloth"})
_CACHED_FULL_MESH_ITEM_KINDS = frozenset({
    "mesh",
    "component_mesh",
    "foliage",
    "grass",
    "collision",
    "rigid",
    "rigid_body",
})
_CACHED_FULL_LIGHT_ITEM_KINDS = frozenset({
    "point_light",
    "spot_light",
    "component_point_light",
    "component_spot_light",
})
_CACHED_FULL_EMPTY_ITEM_KINDS = frozenset({"component_empty", "entity_empty"})
_CACHED_ENTITY_ASSET_ITEM_KINDS = frozenset({"entity_asset"})
_CACHED_SECTOR_INSTANCER_KINDS = frozenset({"sector_instancer"})
_CACHED_FULL_ITEM_KINDS = (
    _CACHED_FULL_MESH_ITEM_KINDS
    | _CACHED_REDCLOTH_ITEM_KINDS
    | _CACHED_FULL_LIGHT_ITEM_KINDS
    | _CACHED_FULL_EMPTY_ITEM_KINDS
    | _CACHED_ENTITY_ASSET_ITEM_KINDS
    | _CACHED_SECTOR_INSTANCER_KINDS
)
_CACHED_FULL_PARENT_ITEM_KINDS = frozenset({"group", "entity"})
_SECTOR_FLAG_MESH_VISIBLE = 1 << 2
_SECTOR_FLAG_MESH_PART_OF_ENTITY_PROXY = 1 << 10
_SECTOR_FLAG_MESH_ROOT_ENTITY_PROXY = 1 << 11


def _sector_flags_value(flags):
    try:
        return int(flags)
    except Exception:
        return None


def _sector_mesh_visible_from_flags(flags, default=True):
    value = _sector_flags_value(flags)
    if value is None:
        return bool(default)
    return bool(value & _SECTOR_FLAG_MESH_VISIBLE)


def _sector_visibility_key_from_flags(flags, default=True):
    return "visible" if _sector_mesh_visible_from_flags(flags, default=default) else "hidden"


def _drawable_flags_visible_from_value(flags, default=True):
    if flags is None:
        return bool(default)
    if hasattr(flags, "strings"):
        return _drawable_flags_visible_from_value(getattr(flags, "strings", None), default=default)
    if hasattr(flags, "Value"):
        return _drawable_flags_visible_from_value(getattr(flags, "Value", None), default=default)
    if isinstance(flags, (list, tuple, set)):
        names = {str(value or "").strip() for value in flags}
        return "DF_IsVisible" in names or "IsVisible" in names
    if isinstance(flags, str):
        parts = {part for part in re.split(r"[\s,|;]+", flags.strip()) if part}
        return "DF_IsVisible" in parts or "IsVisible" in parts
    try:
        return bool(int(flags) & 1)
    except Exception:
        return bool(default)


def _drawable_flags_cast_shadows_from_value(flags, default=False):
    if flags is None:
        return bool(default)
    if hasattr(flags, "strings"):
        return _drawable_flags_cast_shadows_from_value(getattr(flags, "strings", None), default=default)
    if hasattr(flags, "Value"):
        return _drawable_flags_cast_shadows_from_value(getattr(flags, "Value", None), default=default)
    if isinstance(flags, (list, tuple, set)):
        values = {str(value or "").strip() for value in flags}
        return bool({"DF_CastShadows", "DF_CastShadowsFromLocalLightsOnly"} & values)
    if isinstance(flags, str):
        values = {part for part in re.split(r"[\s,|;]+", flags.strip()) if part}
        return bool({"DF_CastShadows", "DF_CastShadowsFromLocalLightsOnly"} & values)
    try:
        return bool(int(flags) & (2 | 1024))
    except Exception:
        return bool(default)


def _drawable_flags_local_only_from_value(flags):
    if flags is None:
        return False
    if hasattr(flags, "strings"):
        return _drawable_flags_local_only_from_value(getattr(flags, "strings", None))
    if hasattr(flags, "Value"):
        return _drawable_flags_local_only_from_value(getattr(flags, "Value", None))
    if isinstance(flags, (list, tuple, set)):
        return "DF_CastShadowsFromLocalLightsOnly" in {
            str(value or "").strip() for value in flags
        }
    if isinstance(flags, str):
        return "DF_CastShadowsFromLocalLightsOnly" in {
            part for part in re.split(r"[\s,|;]+", flags.strip()) if part
        }
    try:
        return bool(int(flags) & 1024)
    except Exception:
        return False


def _component_drawable_flags(component):
    prop = None
    try:
        prop = component.GetVariableByName("drawableFlags")
    except Exception:
        prop = None
    if prop is None:
        try:
            prop = getattr(component, "drawableFlags", None)
        except Exception:
            prop = None
    if prop is None:
        return None
    if hasattr(prop, "strings"):
        return list(getattr(prop, "strings", []) or [])
    if hasattr(prop, "Value"):
        return getattr(prop, "Value", None)
    return prop


def _component_prop_string(component, prop_name):
    try:
        prop = component.GetVariableByName(prop_name)
    except Exception:
        prop = None
    if prop is None:
        return ""
    try:
        value = prop.ToString()
        if value:
            return str(value).strip()
    except Exception:
        pass
    try:
        return str(prop.String.String or "").strip()
    except Exception:
        return ""


def _component_display_label(component):
    component_name = getattr(component, "name", getattr(component, "Type", "")) or "Component"
    label = _component_prop_string(component, "name")
    return f"{component_name} {label}".strip() if label else str(component_name)


def _component_action_name(component):
    return _component_prop_string(component, "actionName")


_ENTITY_LIGHT_PROPERTY_NAMES = (
    "radius",
    "brightness",
    "attenuation",
    "color",
    "envColorGroup",
    "shadowCastingMode",
    "lightFlickering",
    "lightUsageMask",
    "isEnabled",
    "innerAngle",
    "outerAngle",
    "softness",
)


@dataclass
class EntityImportResult:
    root_object: object | None = None
    main_armature: object | None = None
    created_objects: list = field(default_factory=list)
    materialized_item_ids: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def main_object(self):
        return self.main_armature or self.root_object


def _instance_entry_value(entry, name, default=None):
    if isinstance(entry, dict):
        return entry.get(name, default)
    return getattr(entry, name, default)


def _normalize_entity_buffer_v2(buffer_v2):
    entries = _instance_entry_value(buffer_v2, "elements", buffer_v2)
    if not isinstance(entries, (list, tuple)):
        raise ValueError("BufferV2 is not a decoded entry list")
    normalized = []
    for entry in entries:
        component_name_value = _instance_entry_value(
            entry,
            "component_name",
            _instance_entry_value(entry, "componentName", ""),
        )
        component_name = str(
            getattr(component_name_value, "value", component_name_value) or ""
        ).strip()
        if not component_name:
            raise ValueError("BufferV2 entry has no componentName")
        variables = _instance_entry_value(entry, "variables", ())
        variables = _instance_entry_value(variables, "elements", variables)
        if not isinstance(variables, (list, tuple)):
            raise ValueError(f"BufferV2 component {component_name} has no decoded variables")
        normalized_variables = []
        for variable in variables:
            prop = _instance_entry_value(variable, "PROP", variable)
            prop_name = str(
                _instance_entry_value(prop, "name", _instance_entry_value(prop, "theName", ""))
                or ""
            ).strip()
            prop_type = str(
                _instance_entry_value(prop, "type", _instance_entry_value(prop, "theType", ""))
                or ""
            ).strip()
            if not prop_name or not prop_type:
                raise ValueError(f"BufferV2 component {component_name} has an undecoded variable")
            value = (
                _instance_entry_value(prop, "value")
                if isinstance(prop, dict) and "value" in prop
                else read_prop_value(prop, ())
            )
            normalized_variables.append({
                "name": prop_name,
                "type": prop_type,
                "value": _json_safe_plan_value(value),
            })
        normalized.append({
            "component_name": component_name,
            "variables": normalized_variables,
        })
    return normalized


def _entity_instance_overrides(entity):
    overrides = {}
    buffer_v1 = getattr(entity, "BufferV1", None)
    if buffer_v1:
        overrides["BufferV1"] = _json_safe_plan_value(buffer_v1)
    buffer_v2 = getattr(entity, "BufferV2", None)
    if buffer_v2:
        try:
            overrides["BufferV2"] = _normalize_entity_buffer_v2(buffer_v2)
        except Exception as exc:
            overrides["BufferV2"] = {
                "error": str(exc),
                "raw": _json_safe_plan_value(buffer_v2),
            }
    return overrides


def _entity_instance_override_shape_error(overrides):
    if not overrides:
        return ""
    if not isinstance(overrides, dict):
        return "instance_overrides is not an object"
    if overrides.get("BufferV1"):
        return "BufferV1 additional component data is opaque and cannot be applied"
    buffer_v2 = overrides.get("BufferV2")
    if not buffer_v2:
        return ""
    if isinstance(buffer_v2, dict):
        return f"BufferV2 could not be decoded: {buffer_v2.get('error', 'invalid data')}"
    if not isinstance(buffer_v2, list):
        return "BufferV2 is not a decoded entry list"
    for entry in buffer_v2:
        if not isinstance(entry, dict) or not str(entry.get("component_name", "") or "").strip():
            return "BufferV2 contains an invalid component entry"
        variables = entry.get("variables")
        if not isinstance(variables, list):
            return f"BufferV2 component {entry.get('component_name')} has invalid variables"
        for variable in variables:
            if (
                not isinstance(variable, dict)
                or not str(variable.get("name", "") or "").strip()
                or not str(variable.get("type", "") or "").strip()
                or "value" not in variable
            ):
                return f"BufferV2 component {entry.get('component_name')} has an invalid variable"
    return ""


def _iter_entity_override_components(entity):
    static_meshes = getattr(entity, "staticMeshes", None)
    chunks = _instance_entry_value(static_meshes, "chunks", ())
    for chunk in chunks or ():
        yield chunk
    moving = getattr(entity, "MovingPhysicalAgentComponent", None)
    if moving:
        yield moving
    for appearance in getattr(entity, "appearances", None) or ():
        for template in _instance_entry_value(appearance, "includedTemplates", ()) or ():
            for chunk in _instance_entry_value(template, "chunks", ()) or ():
                yield chunk


def _apply_entity_instance_overrides(entity, overrides):
    error = _entity_instance_override_shape_error(overrides)
    if error:
        return error
    components = list(_iter_entity_override_components(entity))
    for entry in (overrides or {}).get("BufferV2", ()):
        component_name = str(entry["component_name"]).strip()
        matches = [
            component
            for component in components
            if str(_instance_entry_value(component, "name", "") or "").strip().lower()
            == component_name.lower()
        ]
        if not matches:
            return f"BufferV2 component {component_name} is not present in embedded entity_data"
        for component in matches:
            for variable in entry["variables"]:
                value = copy.deepcopy(variable["value"])
                if isinstance(component, dict):
                    component[variable["name"]] = value
                else:
                    setattr(component, variable["name"], value)
    return ""


def _merge_unique_entity_metadata(base_values, overlay_values, key_fields=(), *, replace_existing=False):
    merged = list(base_values or [])
    key_indices = {}

    def entry_key(entry):
        if key_fields:
            values = tuple(
                str(_instance_entry_value(entry, field_name, "") or "").strip().lower()
                for field_name in key_fields
            )
            if any(values):
                return ("fields", values)
        return ("json", json.dumps(entry, sort_keys=True, separators=(",", ":"), default=str))

    for index, entry in enumerate(merged):
        key_indices[entry_key(entry)] = index
    for entry in overlay_values or []:
        key = entry_key(entry)
        existing_index = key_indices.get(key)
        if existing_index is not None:
            if replace_existing:
                merged[existing_index] = entry
            continue
        key_indices[key] = len(merged)
        merged.append(entry)
    return merged


def _merge_entity_appearance_metadata(base_appearances, overlay_appearances):
    merged = list(base_appearances or [])
    by_name = {
        str(_instance_entry_value(appearance, "name", "") or "").strip().lower(): index
        for index, appearance in enumerate(merged)
        if str(_instance_entry_value(appearance, "name", "") or "").strip()
    }
    for appearance in overlay_appearances or []:
        name_key = str(_instance_entry_value(appearance, "name", "") or "").strip().lower()
        existing_index = by_name.get(name_key) if name_key else None
        if (
            existing_index is None
            or not isinstance(merged[existing_index], dict)
            or not isinstance(appearance, dict)
        ):
            merged.append(appearance)
            if name_key:
                by_name[name_key] = len(merged) - 1
            continue
        existing = merged[existing_index]
        combined = dict(existing)
        combined.update(appearance)
        combined["includedTemplates"] = _merge_unique_entity_metadata(
            existing.get("includedTemplates"),
            appearance.get("includedTemplates"),
        )
        merged[existing_index] = combined
    return merged


def _merge_instance_entity_metadata(template_data, instance_data):
    if not isinstance(template_data, dict) or not isinstance(instance_data, dict):
        return template_data, instance_data

    template_data = dict(template_data)
    instance_data = dict(instance_data)
    template_data["appearances"] = _merge_entity_appearance_metadata(
        template_data.get("appearances"),
        instance_data.get("appearances"),
    )
    instance_data["appearances"] = []

    template_data["coloringEntries"] = _merge_unique_entity_metadata(
        template_data.get("coloringEntries"),
        instance_data.get("coloringEntries"),
        ("appearance", "componentName"),
        replace_existing=True,
    )
    template_data["slots"] = _merge_unique_entity_metadata(
        template_data.get("slots"),
        instance_data.get("slots"),
        ("name", "componentName", "boneName"),
        replace_existing=True,
    )
    template_data["cookedEffects"] = _merge_unique_entity_metadata(
        template_data.get("cookedEffects"),
        instance_data.get("cookedEffects"),
        ("name",),
        replace_existing=True,
    )
    instance_component_names = _entity_data_override_component_names(instance_data)
    instance_data["coloringEntries"] = [
        entry for entry in instance_data.get("coloringEntries") or []
        if str(_instance_entry_value(entry, "componentName", "") or "").strip().lower()
        in instance_component_names
    ]

    list_fields = {
        "CAnimAnimsetsParam": (),
        "CAnimMimicParam": (),
        "inventoryDefinitions": (),
        "beh_paths": (),
        "included_template_paths": (),
        "template_dependency_paths": (),
    }
    for field_name, key_fields in list_fields.items():
        template_data[field_name] = _merge_unique_entity_metadata(
            template_data.get(field_name),
            instance_data.get(field_name),
            key_fields,
        )
        instance_data[field_name] = []

    body_states = dict(template_data.get("w2_body_part_states") or {})
    body_states.update(instance_data.get("w2_body_part_states") or {})
    template_data["w2_body_part_states"] = body_states
    instance_data["w2_body_part_states"] = {}
    if instance_data.get("isLightOn") is not None:
        template_data["isLightOn"] = instance_data.get("isLightOn")
        instance_data["isLightOn"] = None
    instance_static_meshes = instance_data.get("staticMeshes") or {}
    has_instance_component_graph = bool(
        _instance_entry_value(instance_static_meshes, "chunks", ())
        or instance_data.get("MovingPhysicalAgentComponent")
    )
    if not has_instance_component_graph:
        # Avoid a metadata-only overlay after materializing the template.
        instance_data["slots"] = []
        instance_data["coloringEntries"] = []
        instance_data["cookedEffects"] = []
    return template_data, instance_data


def _entity_data_override_component_names(entity_data):
    if not isinstance(entity_data, dict):
        return set()
    names = set()

    def add_component(component):
        name = str(_instance_entry_value(component, "name", "") or "").strip().lower()
        if name:
            names.add(name)

    static_meshes = entity_data.get("staticMeshes") or {}
    for component in _instance_entry_value(static_meshes, "chunks", ()) or ():
        add_component(component)
    moving = entity_data.get("MovingPhysicalAgentComponent")
    if moving:
        add_component(moving)
    for appearance in entity_data.get("appearances") or ():
        for template in _instance_entry_value(appearance, "includedTemplates", ()) or ():
            for component in _instance_entry_value(template, "chunks", ()) or ():
                add_component(component)
    return names


def _partition_entity_instance_overrides(overrides, *entity_data_values):
    partitions = [{} for _value in entity_data_values]
    if not overrides:
        return partitions, []
    if not isinstance(overrides, dict):
        return partitions, ["instance_overrides is not an object"]
    usable_indices = [
        index for index, entity_data in enumerate(entity_data_values)
        if isinstance(entity_data, dict)
    ]
    if not usable_indices:
        return partitions, ["instance overrides have no embedded entity asset"]

    first_index = usable_indices[0]
    if _entity_instance_override_shape_error(overrides):
        partitions[first_index] = copy.deepcopy(overrides)
        return partitions, []
    for key, value in overrides.items():
        if key != "BufferV2":
            partitions[first_index][key] = copy.deepcopy(value)

    component_names = [
        _entity_data_override_component_names(entity_data)
        for entity_data in entity_data_values
    ]
    errors = []
    for entry in overrides.get("BufferV2", ()) or ():
        component_name = str(entry.get("component_name", "") or "").strip()
        owners = [
            index for index, names in enumerate(component_names)
            if component_name.lower() in names
        ]
        if not owners:
            errors.append(
                f"BufferV2 component {component_name or '<unknown>'} is absent from template and instance graphs"
            )
            continue
        if len(owners) > 1:
            errors.append(
                f"BufferV2 component {component_name} is ambiguous across template and instance graphs"
            )
            continue
        partitions[owners[0]].setdefault("BufferV2", []).append(copy.deepcopy(entry))
    return partitions, errors


@dataclass(frozen=True)
class EntityInstanceSpec:
    identity: str = ""
    name: str = ""
    entity_class: str = "CEntity"
    template_path: str = ""
    source_path: str = ""
    transform: dict | None = None
    parent_id: str = ""
    world_position: tuple | None = None
    direct_components: tuple = ()
    streamed_components: object | None = None
    instance_overrides: dict = field(default_factory=dict)
    guid: str = ""
    entity_id: str = ""
    action_name: str = ""
    engine_visible: bool | None = None
    compiled_entity_asset: object | None = None
    compiled_entity_error: str = ""

    @classmethod
    def from_layer_entity(cls, entity, source_path="", parent_id="", world_position=None):
        name = str(getattr(entity, "name", "") or "")
        entity_class = str(getattr(entity, "type", "") or "CEntity")
        guid = str(getattr(entity, "guid", "") or "")
        entity_id = str(getattr(entity, "id", "") or "")
        identity = str(
            guid
            or entity_id
            or f"{entity_class}:{name}"
        )
        visible = getattr(entity, "visible", None)
        return cls(
            identity=identity,
            name=name,
            entity_class=entity_class,
            template_path=str(
                getattr(entity, "templatePath", "")
                if getattr(entity, "isCreatedFromTemplate", False)
                else ""
            ),
            source_path=str(source_path or ""),
            transform=_copy_engine_transform_dict(getattr(entity, "transform", None)),
            parent_id=str(parent_id or ""),
            world_position=_copy_world_position(world_position),
            direct_components=tuple(getattr(entity, "Components", None) or ()),
            streamed_components=getattr(entity, "streamingDataBuffer", None) or None,
            instance_overrides=_entity_instance_overrides(entity),
            guid=guid,
            entity_id=entity_id,
            action_name=_entity_prop_string(entity, "actionName"),
            engine_visible=bool(visible) if visible is not None else None,
            compiled_entity_asset=getattr(entity, "entityAsset", None),
            compiled_entity_error=str(getattr(entity, "entityAssetError", "") or ""),
        )


def _json_safe_plan_value(value, _seen=None):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return {"encoding": "hex", "data": value.hex()}
    if callable(value):
        return None
    _seen = set() if _seen is None else _seen
    value_id = id(value)
    if value_id in _seen:
        return None
    _seen.add(value_id)
    try:
        if isinstance(value, dict):
            return {str(key): _json_safe_plan_value(item, _seen) for key, item in value.items()}
        if isinstance(value, set):
            return [_json_safe_plan_value(item, _seen) for item in sorted(value, key=str)]
        if isinstance(value, (list, tuple)):
            return [_json_safe_plan_value(item, _seen) for item in value]
        serializer = getattr(value, "__json_serializable__", None)
        if callable(serializer):
            return _json_safe_plan_value(serializer(), _seen)
        if hasattr(value, "__dict__"):
            return {
                str(key): _json_safe_plan_value(item, _seen)
                for key, item in vars(value).items()
                if not callable(item)
            }
        try:
            parsed = read_prop_value(value, None)
        except Exception:
            parsed = None
        if parsed is not None and parsed is not value:
            return _json_safe_plan_value(parsed, _seen)
        try:
            return str(value.ToString())
        except Exception:
            return str(value)
    finally:
        _seen.discard(value_id)


def _component_light_properties(component):
    values = {}
    for prop_name in _ENTITY_LIGHT_PROPERTY_NAMES:
        try:
            prop = component.GetVariableByName(prop_name)
        except Exception:
            prop = None
        if prop is not None:
            values[prop_name] = read_prop_value(prop, ())
    return values


def _entity_prop_string(entity, prop_name):
    try:
        prop = entity.GetVariableByName(prop_name)
    except Exception:
        prop = None
    if prop is None:
        try:
            return str(getattr(entity, prop_name, "") or "").strip()
        except Exception:
            return ""
    try:
        value = prop.ToString()
        if value:
            return str(value).strip()
    except Exception:
        pass
    try:
        return str(prop.String.String or "").strip()
    except Exception:
        return ""


def _is_template_preview_entity(entity):
    name = str(getattr(entity, "name", "") or "").strip()
    if "(" in name:
        name = name.split("(", 1)[0].strip()
    return name.lower() == "previewentity"


def _set_redkit_entity_metadata(
    obj,
    entity_type="CEntity",
    *,
    entity_name="",
    template_path="",
    action_name="",
    instance_id="",
    entity_guid="",
    entity_id="",
):
    if obj is None:
        return
    entity_type = str(entity_type or "CEntity").strip() or "CEntity"
    try:
        obj["witcher_type"] = entity_type
        obj["witcher_redkit_class"] = entity_type
        if entity_name:
            obj["witcher_name"] = str(entity_name)
        if template_path:
            obj["template"] = str(template_path)
        if action_name:
            obj["witcher_redkit_actionName"] = str(action_name)
        if instance_id:
            obj["witcher_redkit_instance_id"] = str(instance_id)
        if entity_guid:
            obj["witcher_redkit_guid"] = str(entity_guid)
        if entity_id:
            obj["witcher_redkit_id"] = str(entity_id)
    except Exception:
        pass


def _set_redkit_component_metadata(
    obj,
    component_type="Component",
    *,
    component_name="",
    mesh_path="",
    drawable_flags=None,
    engine_visible=None,
    action_name="",
):
    if obj is None:
        return
    component_type = str(component_type or "Component").strip() or "Component"
    try:
        obj["witcher_type"] = component_type
        obj["witcher_redkit_class"] = component_type
        if component_name:
            obj["witcher_name"] = str(component_name)
        if mesh_path:
            obj["witcher_redkit_mesh_path"] = str(mesh_path)
        if action_name:
            obj["witcher_redkit_actionName"] = str(action_name)
        drawable_flags_text = _drawable_flags_display_value(drawable_flags)
        obj["witcher_redkit_drawableFlags"] = drawable_flags_text if drawable_flags_text else "Unset"
        shadow_default = str(component_type or "") in {
            "CDestructionComponent",
            "CDestructionSystemComponent",
        }
        obj["witcher_drawableFlags_has_DF_CastShadows"] = _drawable_flags_cast_shadows_from_value(
            drawable_flags,
            default=shadow_default,
        )
        obj["witcher_drawableFlags_local_lights_only"] = _drawable_flags_local_only_from_value(
            drawable_flags,
        )
        if engine_visible is not None:
            obj["witcher_layer_engine_visible"] = bool(engine_visible)
            obj["witcher_drawableFlags_has_DF_IsVisible"] = bool(engine_visible)
    except Exception:
        pass


def _component_type_from_plan_item(item, default="CStaticMeshComponent"):
    component_type = str((item or {}).get("component_type", "") or "").strip()
    if component_type:
        return component_type
    name = str((item or {}).get("name", "") or "").strip()
    for candidate in MeshComponent_Type_List:
        if name == candidate or name.startswith(candidate + " "):
            return candidate
    return default


def _mesh_label_from_path(mesh_path):
    mesh_path = str(mesh_path or "").replace("\\", "/").strip()
    return Path(mesh_path).stem if mesh_path else ""


def _component_label_from_parts(component_type, component_name="", mesh_label=""):
    component_type = str(component_type or "Component").strip() or "Component"
    component_name = str(component_name or "").strip()
    mesh_label = str(mesh_label or "").strip()
    label = mesh_label or component_name
    if label and label != component_type:
        return f"{label} ({component_type})"
    return component_type


def _prepare_mesh_as_component_child(mesh):
    if mesh is None:
        return
    try:
        mesh.transform = False
    except Exception:
        pass
    try:
        mesh.matrix = False
    except Exception:
        pass
    try:
        mesh.translation = False
    except Exception:
        pass


def _static_mesh_chunks_from_entity(entity):
    static_meshes = getattr(entity, "staticMeshes", None)
    if static_meshes is None and hasattr(entity, "chunks"):
        static_meshes = entity
    if static_meshes is None:
        return []
    if isinstance(static_meshes, dict):
        return list(static_meshes.get("chunks", []) or [])
    return list(getattr(static_meshes, "chunks", []) or [])


def _chunk_attr_string(chunk, *names):
    for name in names:
        try:
            value = getattr(chunk, name)
        except Exception:
            value = None
        if value:
            return str(value).strip()
    for name in names:
        try:
            value = _component_prop_string(chunk, name)
        except Exception:
            value = ""
        if value:
            return str(value).strip()
    return ""


def _static_mesh_component_paths_from_entity(entity, *, mesh_fbx_uncook_path=None, mesh_uncook_path=None):
    meshes = []
    for chunk in _static_mesh_chunks_from_entity(entity):
        component_name = _component_prop_string(chunk, "name") if hasattr(chunk, "GetVariableByName") else ""
        if not component_name:
            component_name = _chunk_attr_string(chunk, "name")
        component_type = _chunk_attr_string(chunk, "type", "Type")
        if not component_type:
            component_type = str(getattr(chunk, "name", "") or "").strip()
        if component_type not in MeshComponent_Type_List:
            if component_name in MeshComponent_Type_List:
                component_type = component_name
                component_name = ""
            else:
                for candidate in MeshComponent_Type_List:
                    if component_name.startswith(candidate):
                        component_type = candidate
                        break
        if component_type not in MeshComponent_Type_List:
            continue
        mesh_path = _chunk_attr_string(chunk, "mesh", "resource")
        if not mesh_path:
            continue
        mesh = _new_mesh_path(
            mesh_path,
            fbx_uncook_path=mesh_fbx_uncook_path,
            uncook_path=mesh_uncook_path,
            transform=getattr(chunk, "transform", None) or False,
            cr2w_version=getattr(chunk, "version", getattr(chunk, "cr2w_version", None)),
        )
        drawable_flags = _component_drawable_flags(chunk)
        mesh.drawable_flags = drawable_flags
        mesh.engine_visible = _drawable_flags_visible_from_value(drawable_flags, default=True)
        mesh.component_type = component_type
        mesh.component_name = component_name
        mesh.component_action_name = _chunk_attr_string(chunk, "actionName")
        mesh.is_proxy_mesh = _path_indicates_proxy_mesh(mesh_path, component_name)
        meshes.append(mesh)
    return meshes


def _static_mesh_component_paths_from_template_source(
    template_entity,
    template_path="",
    *,
    mesh_fbx_uncook_path=None,
    mesh_uncook_path=None,
    source_context_path="",
):
    meshes = _static_mesh_component_paths_from_entity(
        template_entity,
        mesh_fbx_uncook_path=mesh_fbx_uncook_path,
        mesh_uncook_path=mesh_uncook_path,
    )
    return meshes


def _set_object_local_matrix_direct(obj, local_matrix, parent_inverse=None):
    if obj is None or local_matrix is None:
        return
    try:
        local_matrix = local_matrix.copy()
    except Exception:
        return
    identity = Matrix.Identity(4)
    if parent_inverse is None:
        parent_inverse = identity
    else:
        try:
            parent_inverse = parent_inverse.copy()
        except Exception:
            parent_inverse = identity
    try:
        obj.matrix_parent_inverse = parent_inverse.copy()
    except Exception:
        pass
    # Write basis last; other matrix setters may recalculate it.
    try:
        parent_obj = getattr(obj, "parent", None)
        obj.matrix_world = (
            parent_obj.matrix_world @ parent_inverse @ local_matrix
            if parent_obj is not None else local_matrix.copy()
        )
    except Exception:
        pass
    try:
        obj.matrix_local = local_matrix.copy()
    except Exception:
        pass
    try:
        obj.matrix_parent_inverse = parent_inverse.copy()
        obj.matrix_basis = local_matrix.copy()
    except Exception:
        pass


def _create_redkit_component_empty(
    component_type,
    *,
    component_name="",
    parent_obj=None,
    transform=None,
    target_collection=None,
    mesh_path="",
    drawable_flags=None,
    engine_visible=None,
    action_name="",
    kwargs=None,
):
    display_name = _component_label_from_parts(
        component_type,
        component_name,
        mesh_label=_mesh_label_from_path(mesh_path),
    )
    obj = _create_linked_empty(
        display_name,
        target_collection,
        display_size=0.2,
    )
    if obj is None:
        return None
    if parent_obj is not None:
        obj.parent = parent_obj
        _set_object_local_matrix_direct(obj, Matrix.Identity(4))
    if transform is not None:
        _apply_engine_transform_local(obj, transform)
    _set_redkit_component_metadata(
        obj,
        component_type,
        component_name=component_name,
        mesh_path=mesh_path,
        drawable_flags=drawable_flags,
        engine_visible=engine_visible,
        action_name=action_name,
    )
    if engine_visible is not None and not bool(engine_visible) and _hide_engine_hidden_meshes_enabled(kwargs):
        try:
            obj.hide_viewport = True
            obj.hide_render = True
        except Exception:
            pass
    return obj


def _promote_component_mesh_children(component_obj, mesh_obj, mesh_path):
    if component_obj is None or mesh_obj is None or mesh_obj == component_obj:
        return
    try:
        scene_repo_key = str(mesh_obj.get("repo_path", "") or "").strip()
    except Exception:
        scene_repo_key = ""
    if not scene_repo_key:
        scene_repo_key = str(mesh_path or "").strip()

    for key in ("repo_path", "witcher_source_mesh_path", "witcher_embedded_cmesh_chunk_index"):
        try:
            if key in mesh_obj:
                component_obj[key] = mesh_obj[key]
        except Exception:
            pass
    if scene_repo_key:
        try:
            component_obj["repo_path"] = scene_repo_key
        except Exception:
            pass

    promoted_children = list(getattr(mesh_obj, "children", []) or [])
    for child in promoted_children:
        try:
            local_matrix = child.matrix_local.copy()
        except Exception:
            local_matrix = None
        try:
            child.parent = component_obj
            if local_matrix is not None:
                _set_object_local_matrix_direct(child, local_matrix)
        except Exception:
            pass
        if scene_repo_key:
            try:
                child["repo_path"] = scene_repo_key
            except Exception:
                pass

    try:
        bpy.data.objects.remove(mesh_obj, do_unlink=True)
    except Exception:
        pass
    try:
        _record_duplicate_root(component_obj)
    except Exception:
        pass


def _import_component_mesh_from_mesh(
    mesh,
    errors,
    parent_obj,
    *,
    component_type="CStaticMeshComponent",
    component_name="",
    component_transform=None,
    drawable_flags=None,
    engine_visible=None,
    action_name="",
    target_collection=None,
    keep_lod_meshes=False,
    version=999,
    kwargs=None,
):
    kwargs = dict(kwargs or {})
    mesh_path = _mesh_repo_path(mesh)
    component_obj = _create_redkit_component_empty(
        component_type,
        component_name=component_name,
        parent_obj=parent_obj,
        transform=component_transform,
        target_collection=target_collection,
        mesh_path=mesh_path,
        drawable_flags=drawable_flags,
        engine_visible=engine_visible,
        action_name=action_name,
        kwargs=kwargs,
    )
    if component_obj is None:
        return None

    _tag_single_object_for_layer(
        component_obj,
        kwargs.get("_layer_import_owner"),
    )
    _tag_object_tree_for_plan_item(
        component_obj,
        kwargs.get("_layer_import_plan_item_id"),
        kwargs.get("_layer_import_plan_mode"),
    )

    _prepare_mesh_as_component_child(mesh)
    mesh_obj = import_single_mesh(
        mesh,
        errors,
        component_obj,
        keep_lod_meshes=keep_lod_meshes,
        version=version,
        **kwargs,
    )
    if mesh_obj is None:
        try:
            bpy.data.objects.remove(component_obj, do_unlink=True)
        except Exception:
            pass
        return None
    _promote_component_mesh_children(component_obj, mesh_obj, mesh_path)

    _tag_object_tree_engine_visibility(
        component_obj,
        bool(engine_visible) if engine_visible is not None else True,
        kwargs,
        drawable_flags=drawable_flags,
    )
    _tag_object_tree_drawable_shadows(component_obj, drawable_flags, component_type)
    _set_redkit_component_metadata(
        component_obj,
        component_type,
        component_name=component_name,
        mesh_path=mesh_path,
        drawable_flags=drawable_flags,
        engine_visible=engine_visible,
        action_name=action_name,
    )
    return component_obj


def _import_meshless_component_empty(component, parent_obj, **kwargs):
    component_type = getattr(component, "name", getattr(component, "Type", "")) or "Component"
    component_name = _component_prop_string(component, "name")
    drawable_flags = _component_drawable_flags(component)
    engine_visible = _drawable_flags_visible_from_value(drawable_flags, default=True)
    obj = _create_redkit_component_empty(
        component_type,
        component_name=component_name,
        parent_obj=parent_obj,
        drawable_flags=drawable_flags,
        engine_visible=engine_visible,
        action_name=_component_action_name(component),
        kwargs=kwargs,
    )
    if obj is None:
        return None
    try:
        transform_prop = component.GetVariableByName('transform')
    except Exception:
        transform_prop = None
    if transform_prop is not None:
        _apply_engine_transform_local(obj, transform_prop.EngineTransform)
    try:
        obj["witcher_meshless_component"] = True
    except Exception:
        pass
    _tag_object_tree_for_layer_and_plan(
        obj,
        kwargs.get("_layer_import_owner"),
        kwargs.get("_layer_import_plan_item_id"),
        kwargs.get("_layer_import_plan_mode"),
    )
    return obj


def _drawable_flags_display_value(flags):
    if flags is None:
        return ""
    if hasattr(flags, "strings"):
        return _drawable_flags_display_value(getattr(flags, "strings", None))
    if hasattr(flags, "Value"):
        return _drawable_flags_display_value(getattr(flags, "Value", None))
    if isinstance(flags, (list, tuple, set)):
        return "|".join(str(value or "").strip() for value in flags if str(value or "").strip())
    return str(flags)


def _build_object_children_map(objects=None):
    children_map = {}
    for candidate in (objects if objects is not None else bpy.data.objects):
        parent_id = _object_identity(getattr(candidate, "parent", None))
        if parent_id is not None:
            children_map.setdefault(parent_id, []).append(candidate)
    return children_map


def _tag_entity_empty_engine_visibility_from_children(entity_empty, kwargs=None, children_map=None):
    if entity_empty is None:
        return
    tagged_children = []
    if children_map is not None:
        child_candidates = children_map.get(_object_identity(entity_empty), [])
    else:
        child_candidates = list(getattr(entity_empty, "children", []) or [])
    for child in child_candidates:
        try:
            value = child.get("witcher_layer_engine_visible", None)
        except Exception:
            value = None
        if value is None:
            continue
        tagged_children.append((child, bool(value)))
    if not tagged_children:
        if children_map is not None:
            descendants = []
            stack = list(child_candidates)
            while stack:
                child = stack.pop()
                descendants.append(child)
                stack.extend(children_map.get(_object_identity(child), []))
        else:
            descendants = list(getattr(entity_empty, "children_recursive", []) or [])
        for child in descendants:
            try:
                value = child.get("witcher_layer_engine_visible", None)
            except Exception:
                value = None
            if value is None:
                continue
            tagged_children.append((child, bool(value)))
    if not tagged_children:
        return

    hidden_children = [child for child, visible in tagged_children if not visible]
    all_hidden = len(hidden_children) == len(tagged_children)
    try:
        instance_visible = entity_empty.get("witcher_entity_instance_visible", None)
    except Exception:
        instance_visible = None
    effective_visible = (not all_hidden) and instance_visible is not False
    try:
        entity_empty["witcher_entity_drawable_components"] = len(tagged_children)
        entity_empty["witcher_entity_engine_hidden_components"] = len(hidden_children)
        entity_empty["witcher_layer_engine_visible"] = effective_visible
        drawable_values = []
        for child, _visible in tagged_children:
            value = child.get("witcher_redkit_drawableFlags", child.get("witcher_drawableFlags", None))
            value = str(value or "").strip()
            if value and value not in drawable_values:
                drawable_values.append(value)
        if drawable_values:
            entity_empty["witcher_redkit_drawableFlags"] = ";".join(drawable_values)
    except Exception:
        pass

    if _hide_engine_hidden_meshes_enabled(kwargs):
        try:
            _set_hide_flags(entity_empty, not effective_visible)
        except Exception:
            pass


def _sector_visibility_key_for_item(kind, item):
    kind = str(kind or "").strip().lower()
    if kind not in {"mesh", "rigid", "rigid_body", "sector_instancer"}:
        return ""
    if not isinstance(item, dict) or "sector_flags" not in item:
        return ""
    return _sector_visibility_key_from_flags(item.get("sector_flags"), default=True)


def _hide_engine_hidden_meshes_enabled(kwargs=None, context=None):
    if kwargs and "hide_engine_hidden_meshes" in kwargs:
        return bool(kwargs.get("hide_engine_hidden_meshes"))
    scene = getattr(context or bpy.context, "scene", None)
    scene_settings = getattr(scene, "witcher_file_browser", None) if scene is not None else None
    return bool(getattr(scene_settings, "terrain_layer_hide_engine_hidden_meshes", True))


def _path_indicates_proxy_mesh(repo_path, name=""):
    text = f"{repo_path or ''}/{name or ''}".replace("\\", "/").lower()
    if not text:
        return False
    parts = [part for part in re.split(r"[\\/]+", text) if part]
    tokens = []
    for part in parts:
        stem = Path(part).stem if "." in part else part
        if stem == "no_proxy" or stem.endswith("_no_proxy") or stem.endswith("-no-proxy"):
            continue
        tokens.extend(token for token in re.split(r"[_\-\s]+", stem) if token)
    return any(token == "proxy" for token in tokens)


def _path_extension(repo_path):
    return Path(str(repo_path or "").replace("\\", "/")).suffix.lower()


def _sector_collision_path_is_visual_mesh(repo_path):
    return _path_extension(repo_path) in {".w2mesh", ".mesh"}


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


def _cached_plan_item_is_proxy_mesh(item):
    if not isinstance(item, dict):
        return False
    proxy_role = str(item.get("proxy_role", "") or "").strip().lower()
    if proxy_role == "part":
        return False
    if proxy_role == "root":
        return True
    if bool(item.get("is_proxy_mesh", False)):
        return True
    return _path_indicates_proxy_mesh(item.get("repo_path", ""), item.get("name", ""))


def _proxy_mesh_filter_active(kwargs):
    return "do_import_ProxyMesh" in dict(kwargs or {})


def _redcloth_enabled_for_import(kwargs, context=None):
    global_enabled = bool(get_do_import_redcloth(context or bpy.context))
    if "do_import_Redcloth" in dict(kwargs or {}):
        return bool(kwargs.get("do_import_Redcloth", False)) and global_enabled
    return global_enabled


def _redapex_enabled_for_import(kwargs, context=None):
    if "do_import_Redapex" in dict(kwargs or {}):
        return bool(kwargs.get("do_import_Redapex", False))
    return False


def _is_redapex_resource(resource_path):
    return str(resource_path or "").strip().lower().endswith(".redapex")


def _cloth_resource_enabled_for_import(resource_path, kwargs, context=None):
    if _is_redapex_resource(resource_path):
        return _redapex_enabled_for_import(kwargs, context)
    return _redcloth_enabled_for_import(kwargs, context)


def _redapex_import_options(kwargs):
    return {
        "import_chunks": bool(kwargs.get("redapex_import_chunks", False)),
        "import_floor": bool(kwargs.get("redapex_import_floor", False)),
        "collections_as_empties": bool(kwargs.get("redapex_collections_as_empties", True)),
    }


def _cloth_import_failure_reason(root_obj):
    if root_obj is None:
        return "returned no object"
    try:
        failed_resource = str(root_obj.get("witcher_import_error", "") or "").strip()
    except Exception:
        failed_resource = ""
    if failed_resource:
        return f"returned an error placeholder for {failed_resource}"
    return ""


def _apply_cached_destruction_metadata(root_obj, item, kwargs=None):
    component_type = str(item.get("component_type", "") or "").strip()
    if component_type not in {"CDestructionComponent", "CDestructionSystemComponent"}:
        return
    drawable_flags = item.get("drawable_flags") if "drawable_flags" in item else None
    engine_visible = item.get("engine_visible") if "engine_visible" in item else True
    _tag_object_tree_drawable_shadows(root_obj, drawable_flags, component_type)
    _tag_object_tree_engine_visibility(
        root_obj,
        engine_visible,
        kwargs,
        drawable_flags=drawable_flags,
    )
    _set_redkit_component_metadata(
        root_obj,
        component_type,
        component_name=str(item.get("component_name", "") or ""),
        drawable_flags=drawable_flags,
        engine_visible=engine_visible,
        action_name=str(item.get("action_name", "") or ""),
    )


def _chunk_cloth_resource(chunk):
    for property_name in ("resource", "m_resource"):
        try:
            resource_var = chunk.GetVariableByName(property_name)
            handles = getattr(resource_var, "Handles", None) or []
            if handles:
                resource = str(getattr(handles[0], "DepotPath", "") or "").strip()
                if resource:
                    return resource
        except Exception:
            continue
    return ""


def _new_mesh_path(
    mesh_name=False,
    translation=False,
    matrix=False,
    *,
    fbx_uncook_path=None,
    uncook_path=None,
    transform=False,
    block_data_object_type=Enums.BlockDataObjectType.Mesh,
    cr2w_version=None,
):
    mesh = meshPath(
        meshName=mesh_name,
        translation=translation,
        matrix=matrix,
        fbx_uncook_path=fbx_uncook_path if fbx_uncook_path is not None else False,
        transform=transform,
        BlockDataObjectType=block_data_object_type,
        uncook_path=uncook_path,
    )
    if cr2w_version is not None:
        mesh.cr2w_version = _mesh_cr2w_version(mesh, cr2w_version)
    return mesh


def get_CSectorData(level, *, mesh_fbx_uncook_path=None, mesh_uncook_path=None):
    if level.CSectorData:
        level_version = getattr(level, "version", None)
        #import entities hold import data
        static_mesh_list = []
        # meshPath entities hold a transform and components such as import data.
        # THIS_ENTITY = meshPath("CSectorData_Transform", False, False, fbx_uncook_path, BasicEngineQsTransform())
        # THIS_ENTITY.type = "Entity"
        for idx, block in enumerate(level.CSectorData.BlockData):
            #TESTING
            this_type = Enums.BlockDataObjectType.getEnum(block.packedObjectType)
            if hasattr(block, 'resourceIndex') and block.resourceIndex < 12:
                this_resource = level.CSectorData.Resources[block.resourceIndex].pathHash
                log.debug(str(block.resourceIndex)+' '+this_resource)

            if block.packedObjectType == Enums.BlockDataObjectType.Mesh:# or block.packedObjectType == Enums.BlockDataObjectType.Invalid:
                mesh_path = level.CSectorData.Resources[block.packedObject.meshIndex].pathHash
                #obj_pos = level.CSectorData.Objects[idx].position
                mesh_item = _new_mesh_path(
                    mesh_path,
                    block.position,
                    MatrixToArray(block.rotationMatrix),
                    fbx_uncook_path=mesh_fbx_uncook_path,
                    uncook_path=mesh_uncook_path,
                    cr2w_version=level_version,
                )
                mesh_item.sector_flags = int(getattr(block, "flags", 0) or 0)
                mesh_item.proxy_role = _sector_proxy_role_from_flags(mesh_item.sector_flags)
                mesh_item.is_proxy_mesh = mesh_item.proxy_role == "root" or _path_indicates_proxy_mesh(mesh_path, "")
                static_mesh_list.append(mesh_item)
            if block.packedObjectType == Enums.BlockDataObjectType.RigidBody:
                mesh_path = level.CSectorData.Resources[block.packedObject.meshIndex].pathHash
                mesh_item = _new_mesh_path(
                    mesh_path,
                    block.position,
                    MatrixToArray(block.rotationMatrix),
                    fbx_uncook_path=mesh_fbx_uncook_path,
                    uncook_path=mesh_uncook_path,
                    block_data_object_type=Enums.BlockDataObjectType.RigidBody,
                    cr2w_version=level_version,
                )
                mesh_item.sector_flags = int(getattr(block, "flags", 0) or 0)
                static_mesh_list.append(mesh_item)
                log.info("found RigidBody in CSectorData")
            if block.packedObjectType == Enums.BlockDataObjectType.Collision:
                mesh_path = level.CSectorData.Resources[block.packedObject.meshIndex].pathHash
                mesh_item = _new_mesh_path(
                    mesh_path,
                    block.position,
                    MatrixToArray(block.rotationMatrix),
                    fbx_uncook_path=mesh_fbx_uncook_path,
                    uncook_path=mesh_uncook_path,
                    block_data_object_type=Enums.BlockDataObjectType.Collision,
                    cr2w_version=level_version,
                )
                mesh_item.sector_flags = int(getattr(block, "flags", 0) or 0)
                static_mesh_list.append(mesh_item)
                log.info("found Collision in CSectorData")
            if block.packedObjectType == Enums.BlockDataObjectType.PointLight:
                log.info("found point light in CSectorData")
                static_mesh_list.append(lightObject("PointLight", block.position, MatrixToArray(block.rotationMatrix), block = block, BlockDataObjectType = Enums.BlockDataObjectType.PointLight))
            if block.packedObjectType == Enums.BlockDataObjectType.SpotLight:
                static_mesh_list.append(lightObject("SpotLight", block.position, MatrixToArray(block.rotationMatrix), block = block, BlockDataObjectType = Enums.BlockDataObjectType.SpotLight))
                #light_path = level.CSectorData.Resources[block.resourceIndex].pathHash
                log.info("found spot light in CSectorData")
            if block.packedObjectType == Enums.BlockDataObjectType.Invalid:
                log.info("found point Invalid in CSectorData")
            if block.packedObjectType == Enums.BlockDataObjectType.Cloth:
                log.info("found point Cloth in CSectorData")
            if block.packedObjectType == Enums.BlockDataObjectType.Decal:
                log.info("found point Decal in CSectorData")
            if block.packedObjectType == Enums.BlockDataObjectType.Destruction:
                log.info("found point Destruction in CSectorData")
            if block.packedObjectType == Enums.BlockDataObjectType.Dimmer:
                log.info("found point Dimmer in CSectorData")
            if block.packedObjectType == Enums.BlockDataObjectType.Particles:
                log.info("found point Particles in CSectorData")
        return static_mesh_list
    else:
        return False


def recurLayerCollection(layerColl, collName):
    if layerColl is None:
        return None
    target_collection = collName if hasattr(collName, "name") else None
    target_name = target_collection.name if target_collection is not None else collName
    if target_collection is not None and getattr(layerColl, "collection", None) == target_collection:
        return layerColl
    found = None
    if layerColl.name == target_name:
        return layerColl
    for layer in layerColl.children:
        found = recurLayerCollection(layer, collName)
        if found:
            return found
         
import math
def is_within_distance(mesh_translation, reference_vector, distance_threshold):
    # Calculate the Euclidean distance between the two vectors
    distance = math.sqrt((mesh_translation[0] - reference_vector[0])**2 + 
                        (mesh_translation[1] - reference_vector[1])**2 +
                        (mesh_translation[2] - reference_vector[2])**2)
    
    # Check if the distance is within the threshold
    if distance <= distance_threshold:
        return True
    else:
        return False


def _get_nearby_import_stats(kwargs):
    stats = kwargs.get("_nearby_stats")
    if not isinstance(stats, dict):
        stats = {"filtered": 0}
        kwargs["_nearby_stats"] = stats
    stats["filtered"] = int(stats.get("filtered", 0) or 0)
    return stats


def _note_nearby_filter_skip(nearby_stats):
    nearby_stats["filtered"] = int(nearby_stats.get("filtered", 0) or 0) + 1


def _get_nearby_import_filter(kwargs):
    if "_nearby_filter" in kwargs:
        return kwargs.get("_nearby_filter")

    camera_position = kwargs.get("_nearby_camera_position")
    radius = kwargs.get("_nearby_radius", 0.0)
    nearby_filter = None
    try:
        if camera_position is not None:
            nearby_filter = {
                "camera_position": (
                    float(camera_position[0]),
                    float(camera_position[1]),
                    float(camera_position[2]),
                ),
                "radius": float(radius or 0.0),
            }
            nearby_filter["radius_sq"] = nearby_filter["radius"] * nearby_filter["radius"]
            if nearby_filter["radius"] <= 0.0:
                nearby_filter = None
    except Exception:
        nearby_filter = None

    kwargs["_nearby_filter"] = nearby_filter
    return nearby_filter


def _extract_vector_position(value):
    if value is None:
        return None
    try:
        return float(value.x), float(value.y), float(value.z)
    except Exception:
        pass
    try:
        return float(value[0]), float(value[1]), float(value[2])
    except Exception:
        return None


def _extract_transform_position(transform):
    if transform is None:
        return None
    return (
        _transform_real(transform, "X", 0.0),
        _transform_real(transform, "Y", 0.0),
        _transform_real(transform, "Z", 0.0),
    )


def _compose_world_position(local_position, parent_position=None):
    if local_position is None:
        return parent_position
    if parent_position is None:
        return local_position
    return (
        float(parent_position[0]) + float(local_position[0]),
        float(parent_position[1]) + float(local_position[1]),
        float(parent_position[2]) + float(local_position[2]),
    )


def _position_within_nearby_filter(position, nearby_filter):
    if nearby_filter is None or position is None:
        return True
    camera_position = nearby_filter["camera_position"]
    dx = float(position[0]) - camera_position[0]
    dy = float(position[1]) - camera_position[1]
    return (dx * dx + dy * dy) <= nearby_filter["radius_sq"]


def _mesh_world_position(mesh, parent_position=None):
    translation = _extract_vector_position(getattr(mesh, "translation", None))
    if translation is not None:
        return translation
    return _compose_world_position(
        _extract_transform_position(getattr(mesh, "transform", None)),
        parent_position,
    )


def _entity_world_position(entity, parent_position=None):
    return _compose_world_position(
        _extract_transform_position(getattr(entity, "transform", None)),
        parent_position,
    )


def _chunk_world_position(chunk, parent_position=None):
    if chunk is None or not hasattr(chunk, "GetVariableByName"):
        return parent_position
    try:
        transform_prop = chunk.GetVariableByName("transform")
    except Exception:
        transform_prop = None
    transform = getattr(transform_prop, "EngineTransform", None) if transform_prop else None
    return _compose_world_position(_extract_transform_position(transform), parent_position)


def _copy_engine_transform_dict(transform):
    if transform is None:
        return None
    return {
        "X": _transform_real(transform, "X", 0.0),
        "Y": _transform_real(transform, "Y", 0.0),
        "Z": _transform_real(transform, "Z", 0.0),
        "Yaw": _transform_real(transform, "Yaw", 0.0),
        "Pitch": _transform_real(transform, "Pitch", 0.0),
        "Roll": _transform_real(transform, "Roll", 0.0),
        "Scale_x": _transform_real(transform, "Scale_x", 1.0),
        "Scale_y": _transform_real(transform, "Scale_y", 1.0),
        "Scale_z": _transform_real(transform, "Scale_z", 1.0),
    }


def _copy_matrix_array(matrix_value):
    if matrix_value is None:
        return None
    try:
        rows = []
        for row in matrix_value:
            rows.append(tuple(float(value) for value in row))
        return tuple(rows) if rows else None
    except Exception:
        return None


def _copy_translation_vector(value):
    position = _extract_vector_position(value)
    if position is None:
        return None
    return (
        float(position[0]),
        float(position[1]),
        float(position[2]),
    )


def _copy_world_position(position):
    if position is None:
        return None
    return (
        float(position[0]),
        float(position[1]),
        float(position[2]),
    )


def _new_level_import_plan(mode="parsed_layer"):
    return {
        "mode": str(mode or "parsed_layer"),
        "_next_item_id": 1,
        "items": [],
        "stats": {
            "total": 0,
            "filtered": 0,
            "by_kind": {},
        },
    }


def entity_import_plan_hash(plan):
    items = []
    for item in (plan or {}).get("items", []) or []:
        if isinstance(item, dict) and item.get("entity_data_hash") and "entity_data" in item:
            item = {key: value for key, value in item.items() if key != "entity_data"}
        items.append(item)
    try:
        payload = json.dumps(items, sort_keys=True, separators=(",", ":"))
    except Exception as exc:
        raise ValueError(f"Entity import plan is not JSON-safe: {exc}") from exc
    return hashlib.sha1(payload.encode("utf-8", "ignore")).hexdigest()


def _add_level_import_plan_item(
    plan,
    kind,
    name,
    *,
    parent_id="",
    repo_path="",
    transform=None,
    matrix=None,
    translation=None,
    world_position=None,
    is_proxy_mesh=None,
    proxy_role="",
    sector_flags=None,
    sector_transforms=None,
    sector_visibility_key="",
    sector_visible=None,
    source_kind="",
    drawable_flags=None,
    engine_visible=None,
    embedded_cmesh_chunk_index=None,
    component_type="",
    component_name="",
    action_name="",
    entity_class="",
    instance_id="",
    entity_guid="",
    entity_id="",
    instance_overrides=None,
    source_path="",
    selected_appearance="",
    entity_data=None,
    entity_data_hash="",
    entity_asset_overlay=None,
    shared_armature_item_id="",
    light_properties=None,
    cr2w_version=None,
    mesh_uncook_path=None,
):
    item_kind = str(kind or "unknown").strip() or "unknown"
    try:
        next_item_index = max(1, int(plan.get("_next_item_id", 1) or 1))
    except Exception:
        next_item_index = 1
    item_id = f"item_{next_item_index}"
    plan["_next_item_id"] = next_item_index + 1
    item = {
        "id": item_id,
        "kind": item_kind,
        "name": str(name or item_kind).strip() or item_kind,
        "parent_id": str(parent_id or "").strip(),
        "repo_path": str(repo_path or "").strip(),
        "transform": _copy_engine_transform_dict(transform),
        "matrix": _copy_matrix_array(matrix),
        "translation": _copy_translation_vector(translation),
        "world_position": _copy_world_position(world_position),
    }
    if is_proxy_mesh is not None:
        item["is_proxy_mesh"] = bool(is_proxy_mesh)
    if proxy_role:
        item["proxy_role"] = str(proxy_role)
    if sector_flags is not None:
        try:
            item["sector_flags"] = int(sector_flags)
        except Exception:
            pass
    if sector_transforms is not None:
        item["sector_transforms"] = list(sector_transforms)
    if sector_visibility_key:
        item["sector_visibility_key"] = str(sector_visibility_key)
    if sector_visible is not None:
        item["sector_visible"] = bool(sector_visible)
    if source_kind:
        item["_source_kind"] = str(source_kind)
    if drawable_flags is not None:
        item["drawable_flags"] = drawable_flags
    if engine_visible is not None:
        item["engine_visible"] = bool(engine_visible)
    if component_type:
        item["component_type"] = str(component_type)
    if component_name:
        item["component_name"] = str(component_name)
    if action_name:
        item["action_name"] = str(action_name)
    if entity_class:
        item["entity_class"] = str(entity_class)
    if instance_id:
        item["instance_id"] = str(instance_id)
    if entity_guid:
        item["entity_guid"] = str(entity_guid)
    if entity_id:
        item["entity_id"] = str(entity_id)
    if instance_overrides:
        item["instance_overrides"] = _json_safe_plan_value(instance_overrides)
    if source_path:
        item["source_path"] = str(source_path)
    if selected_appearance:
        item["selected_appearance"] = str(selected_appearance)
    if entity_data is not None:
        item["entity_data"] = entity_data if entity_data_hash else _json_safe_plan_value(entity_data)
    if entity_data_hash:
        item["entity_data_hash"] = str(entity_data_hash)
    if entity_asset_overlay:
        item["entity_asset_overlay"] = True
    if shared_armature_item_id:
        item["shared_armature_item_id"] = str(shared_armature_item_id)
    if light_properties:
        item.update(light_properties)
    if cr2w_version is not None:
        item["cr2w_version"] = _mesh_cr2w_version(None, cr2w_version)
    if mesh_uncook_path:
        item["mesh_uncook_path"] = str(mesh_uncook_path)
    embedded_cmesh_chunk_index = _normalize_embedded_cmesh_chunk_index(embedded_cmesh_chunk_index)
    if embedded_cmesh_chunk_index is not None:
        item["embedded_cmesh_chunk_index"] = embedded_cmesh_chunk_index
    plan["items"].append(item)
    plan["stats"]["total"] = len(plan["items"])
    by_kind = plan["stats"]["by_kind"]
    by_kind[item_kind] = int(by_kind.get(item_kind, 0) or 0) + 1
    return item["id"]


def _remove_level_import_plan_item(plan, item_id):
    if not item_id:
        return
    removed_item = None
    for index, item in enumerate(plan["items"]):
        if item.get("id") == item_id:
            removed_item = plan["items"].pop(index)
            break
    if removed_item is None:
        return
    kind = str(removed_item.get("kind", "") or "").strip()
    if kind:
        by_kind = plan["stats"]["by_kind"]
        remaining = int(by_kind.get(kind, 0) or 0) - 1
        if remaining > 0:
            by_kind[kind] = remaining
        else:
            by_kind.pop(kind, None)
    plan["stats"]["total"] = len(plan["items"])


def _create_linked_empty(name, target_collection=None, *, display_size=0.25):
    target_collection = target_collection or _get_active_collection()
    if target_collection is None:
        return None
    obj = bpy.data.objects.new(str(name or "Empty"), None)
    obj.empty_display_type = 'PLAIN_AXES'
    obj.empty_display_size = float(display_size)
    target_collection.objects.link(obj)
    return obj


def _apply_plan_item_transform(obj, item):
    transform = item.get("transform")
    if transform:
        set_blender_object_transform(obj, transform)

    matrix_rows = item.get("matrix")
    if matrix_rows:
        mat = Matrix.Identity(4)
        try:
            for row_index, row in enumerate(matrix_rows):
                if row_index >= 4:
                    break
                for col_index, value in enumerate(row):
                    if col_index >= 4:
                        break
                    mat[row_index][col_index] = float(value)
            obj.matrix_basis = mat
        except Exception:
            pass

    translation = item.get("translation")
    if translation is not None:
        try:
            obj.location[0] = float(translation[0])
            obj.location[1] = float(translation[1])
            obj.location[2] = float(translation[2])
        except Exception:
            pass


def _engine_transform_to_local_matrix(engine_transform, rotate_180=False):
    yaw = _transform_real(engine_transform, "Yaw", 0.0)
    pitch = _transform_real(engine_transform, "Pitch", 0.0)
    roll = _transform_real(engine_transform, "Roll", 0.0)
    loc_x = _transform_real(engine_transform, "X", 0.0)
    loc_y = _transform_real(engine_transform, "Y", 0.0)
    loc_z = _transform_real(engine_transform, "Z", 0.0)

    if yaw == 0.0 and pitch == 0.0 and roll == 0.0:
        mat = Matrix.Identity(4)
    else:
        mat = Euler((radians(yaw), radians(pitch), radians(roll)), 'YXZ').to_matrix().to_4x4()
        if rotate_180:
            mat[0][0], mat[0][1], mat[0][2] = -mat[0][0], -mat[0][1], mat[0][2]
            mat[1][0], mat[1][1], mat[1][2] = -mat[1][0], -mat[1][1], mat[1][2]
            mat[2][0], mat[2][1], mat[2][2] = -mat[2][0], -mat[2][1], mat[2][2]

    mat.translation = (loc_x, loc_y, loc_z)
    return mat


def _apply_engine_transform_local(obj, engine_transform, rotate_180=False):
    if obj is None or engine_transform is None:
        return
    local_matrix = _engine_transform_to_local_matrix(engine_transform, rotate_180=rotate_180)
    if isinstance(engine_transform, dict) or hasattr(engine_transform, "Scale_x"):
        local_matrix = local_matrix @ Matrix.Diagonal((
            _transform_real(engine_transform, "Scale_x", 1.0),
            _transform_real(engine_transform, "Scale_y", 1.0),
            _transform_real(engine_transform, "Scale_z", 1.0),
            1.0,
        ))
    _set_object_local_matrix_direct(obj, local_matrix)


def _apply_plan_item_transform_as_child(obj, item):
    transform = item.get("transform")
    if transform:
        _apply_engine_transform_local(obj, transform)
        return
    _apply_plan_item_transform(obj, item)


def _apply_plan_item_transform_for_parent(obj, item, parent_obj):
    if parent_obj is not None:
        _apply_plan_item_transform_as_child(obj, item)
    else:
        _apply_plan_item_transform(obj, item)


def _tag_single_object_for_layer(obj, owner_tag=None):
    if obj is None:
        return
    owner_tag = str(owner_tag or "").strip()
    try:
        if owner_tag:
            obj[_LAYER_IMPORT_OWNER_PROP] = owner_tag
    except Exception:
        pass


def _tag_object_tree_for_plan_item(root_obj, item_id, mode_signature=""):
    _tag_object_tree_for_layer_and_plan(
        root_obj,
        item_id=item_id,
        mode_signature=mode_signature,
    )


def _cached_plan_loaded_item_ids(target_collection, mode_signature=""):
    return set(_cached_plan_loaded_item_objects(target_collection, mode_signature).keys())


def _cached_plan_loaded_item_objects(target_collection, mode_signature=""):
    loaded = {}
    mode_signature = str(mode_signature or "").strip()
    if target_collection is None:
        return loaded
    for obj in list(getattr(target_collection, "all_objects", []) or []):
        try:
            item_id = str(obj.get(_LAYER_IMPORT_PLAN_ITEM_PROP, "") or "").strip()
            if not item_id:
                continue
            obj_mode = str(obj.get(_LAYER_IMPORT_PLAN_MODE_PROP, "") or "").strip()
            if mode_signature and obj_mode and obj_mode != mode_signature:
                continue
            loaded.setdefault(item_id, obj)
        except Exception:
            continue
    return loaded


def _cached_plan_filter_for_position(camera_position=None, radius=0.0):
    if camera_position is None:
        return None
    try:
        radius_value = float(radius or 0.0)
        if radius_value <= 0.0:
            return None
        return {
            "camera_position": (
                float(camera_position[0]),
                float(camera_position[1]),
                float(camera_position[2]),
            ),
            "radius": radius_value,
            "radius_sq": radius_value * radius_value,
        }
    except Exception:
        return None


def _embedded_entity_data_has_complete_appearance_templates(entity_data):
    if not isinstance(entity_data, dict):
        return False
    for appearance in entity_data.get("appearances", []) or []:
        if not isinstance(appearance, dict):
            return False
        if bool(appearance.get("_dlc_mounter_lazy", False)):
            return False
        for template in appearance.get("includedTemplates", []) or []:
            if isinstance(template, dict):
                if (
                    not (template.get("chunks") or [])
                    and str(template.get("templateFilename", "") or "").strip()
                    and not bool(template.get("plan_complete", False))
                ):
                    return False
            elif (
                not (getattr(template, "chunks", None) or [])
                and str(getattr(template, "templateFilename", "") or "").strip()
                and not bool(getattr(template, "plan_complete", False))
            ):
                return False
    return True


def _embedded_entity_data_missing_w2_mimic_skeleton(entity_data):
    def visit(value):
        if isinstance(value, dict):
            try:
                embedded_index = int(
                    value.get("w2_mimic_skeleton_embedded_chunk_index", -1)
                )
            except Exception:
                embedded_index = -1
            if (
                embedded_index >= 0
                and not value.get("w2_mimic_embedded_skeleton_data")
            ):
                return True
            return any(visit(child) for child in value.values())
        if isinstance(value, (list, tuple)):
            return any(visit(child) for child in value)
        return False

    return visit(entity_data)


def _embedded_entity_data_validation_error(entity_data, instance_overrides=None, deep=True):
    if not isinstance(entity_data, dict):
        return "embedded entity_data is not an object"
    if not str(entity_data.get("name", "") or "").strip():
        return "embedded entity_data has no entity name"
    if _embedded_entity_data_missing_w2_mimic_skeleton(entity_data):
        return "embedded Witcher 2 mimic skeleton data is incomplete"
    if not deep:
        return _entity_instance_override_shape_error(instance_overrides) if instance_overrides else None
    try:
        from ..CR2W import w3_types

        entity = w3_types.Entity.from_json(copy.deepcopy(entity_data))
    except Exception as exc:
        return f"embedded entity_data cannot be restored: {exc}"
    if entity is None:
        return "embedded entity_data restored to no entity"
    return _apply_entity_instance_overrides(entity, instance_overrides)


def _embedded_entity_data_unsupported_components(entity_data):
    if not isinstance(entity_data, dict):
        return []
    return [str(value) for value in (entity_data.get("unsupported_components") or []) if str(value)]


def _entity_import_plan_preflight(plan, deep=True):
    items = (plan.get("items", []) if isinstance(plan, dict) else plan) or []

    fatal = []
    item_errors = {}

    def add_item_error(item_id, message):
        if item_id:
            item_errors.setdefault(item_id, []).append(message)
        else:
            fatal.append(message)

    item_ids = set()
    parent_by_id = {}
    kind_by_id = {}
    item_order = {}
    item_by_id = {}
    entity_asset_override_parent_ids = {
        str(item.get("parent_id", "") or "").strip()
        for item in items
        if isinstance(item, dict)
        and str(item.get("kind", "") or "").strip().lower() in _CACHED_ENTITY_ASSET_ITEM_KINDS
        and (item.get("instance_overrides") or {}).get("BufferV2")
    }
    for item_index, item in enumerate(items):
        if not isinstance(item, dict):
            fatal.append("plan contains a non-object item")
            continue
        item_id = str(item.get("id", "") or "").strip()
        kind = str(item.get("kind", "") or "").strip().lower()
        if not item_id:
            fatal.append("plan item is missing an id")
        elif item_id in item_ids:
            fatal.append(f"duplicate plan item id: {item_id}")
        item_ids.add(item_id)
        if item_id:
            parent_by_id[item_id] = str(item.get("parent_id", "") or "").strip()
            kind_by_id[item_id] = kind
            item_order[item_id] = item_index
            item_by_id[item_id] = item
        if kind not in _CACHED_FULL_ITEM_KINDS and kind not in _CACHED_FULL_PARENT_ITEM_KINDS:
            add_item_error(item_id, f"unsupported plan item kind: {kind or '<empty>'}")
        entity_data = item.get("entity_data")
        if kind in _CACHED_ENTITY_ASSET_ITEM_KINDS and not isinstance(entity_data, dict):
            add_item_error(
                item_id,
                f"plan item {item_id or '<unknown>'} requires embedded entity_data",
            )
        elif (
            kind in _CACHED_ENTITY_ASSET_ITEM_KINDS
            and not _embedded_entity_data_has_complete_appearance_templates(entity_data)
        ):
            add_item_error(
                item_id,
                f"plan item {item_id or '<unknown>'} has incomplete embedded appearance templates",
            )
        if kind in _CACHED_ENTITY_ASSET_ITEM_KINDS and entity_data is not None:
            validation_error = _embedded_entity_data_validation_error(
                entity_data,
                item.get("instance_overrides"),
                deep=deep,
            )
            if validation_error:
                add_item_error(item_id, f"plan item {item_id or '<unknown>'}: {validation_error}")
        elif item.get("instance_overrides"):
            validation_error = _entity_instance_override_shape_error(item.get("instance_overrides"))
            if validation_error:
                add_item_error(item_id, f"plan item {item_id or '<unknown>'}: {validation_error}")
            elif (
                (item.get("instance_overrides") or {}).get("BufferV2")
                and item_id not in entity_asset_override_parent_ids
            ):
                add_item_error(
                    item_id,
                    f"plan item {item_id or '<unknown>'} has BufferV2 overrides but no embedded entity asset",
                )
        if kind in (_CACHED_FULL_MESH_ITEM_KINDS | _CACHED_REDCLOTH_ITEM_KINDS | _CACHED_SECTOR_INSTANCER_KINDS):
            if not str(item.get("repo_path", "") or item.get("source_path", "") or "").strip():
                add_item_error(item_id, f"plan item {item_id or '<unknown>'} has no resource path")

    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id", "") or "").strip()
        parent_id = str(item.get("parent_id", "") or "").strip()
        if parent_id and parent_id not in item_ids:
            add_item_error(item_id, f"plan item {item.get('id', '<unknown>')} has missing parent {parent_id}")
        shared_asset_id = str(item.get("shared_armature_item_id", "") or "").strip()
        if bool(item.get("entity_asset_overlay", False)) and not shared_asset_id:
            add_item_error(item_id, f"plan item {item_id or '<unknown>'} is an overlay without a shared entity asset")
        if shared_asset_id:
            if kind_by_id.get(shared_asset_id) != "entity_asset":
                add_item_error(
                    item_id,
                    f"plan item {item_id or '<unknown>'} has invalid shared entity asset {shared_asset_id}",
                )
            elif item_order.get(shared_asset_id, -1) >= item_order.get(item_id, -1):
                add_item_error(
                    item_id,
                    f"plan item {item_id or '<unknown>'} must follow shared entity asset {shared_asset_id}",
                )
            else:
                shared_item = item_by_id[shared_asset_id]
                if str(shared_item.get("parent_id", "") or "").strip() != parent_id:
                    add_item_error(
                        item_id,
                        f"plan item {item_id or '<unknown>'} cannot share an armature across entity parents",
                    )
                shared_instance_id = str(shared_item.get("instance_id", "") or "").strip()
                instance_id = str(item.get("instance_id", "") or "").strip()
                if shared_instance_id and instance_id and shared_instance_id != instance_id:
                    add_item_error(
                        item_id,
                        f"plan item {item_id or '<unknown>'} cannot share an armature across entity instances",
                    )
    reported_cycles = set()
    for item_id in parent_by_id:
        path = []
        path_indices = {}
        current_id = item_id
        while current_id:
            if current_id in path_indices:
                cycle = path[path_indices[current_id]:]
                cycle_key = tuple(sorted(cycle))
                if cycle_key not in reported_cycles:
                    reported_cycles.add(cycle_key)
                    fatal.append(f"plan parent cycle: {' -> '.join(cycle + [current_id])}")
                break
            if current_id not in parent_by_id:
                break
            path_indices[current_id] = len(path)
            path.append(current_id)
            current_id = parent_by_id.get(current_id, "")
    return fatal, item_errors


def _entity_import_plan_preflight_errors(plan):
    fatal, item_errors = _entity_import_plan_preflight(plan)
    return list(fatal) + [message for messages in item_errors.values() for message in messages]


def _entity_plan_skip_ids(items, item_errors):
    if not item_errors:
        return set()
    skip = set(item_errors)
    changed = True
    while changed:
        changed = False
        for item in items:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id", "") or "").strip()
            if not item_id or item_id in skip:
                continue
            parent_id = str(item.get("parent_id", "") or "").strip()
            shared_id = str(item.get("shared_armature_item_id", "") or "").strip()
            if (parent_id and parent_id in skip) or (shared_id and shared_id in skip):
                skip.add(item_id)
                changed = True
    return skip


def _log_entity_plan_unsupported(level_file, plan):
    unsupported = [
        str(value) for value in ((plan or {}).get("unsupported", []) or []) if str(value)
    ]
    if unsupported:
        log.warning(
            "Layer %s has %d unsupported entity note(s), importing the rest: %s",
            level_file,
            len(unsupported),
            _preview_list(unsupported, limit=4),
        )


def entity_import_plan_can_materialize(plan, camera_position=None, radius=0.0, import_kwargs=None, context=None):
    nearby_filter = _cached_plan_filter_for_position(camera_position, radius)
    nearby_stats = {"filtered": 0}
    plan_items = plan.get("items", []) if isinstance(plan, dict) else plan
    source_items = [item for item in plan_items or [] if isinstance(item, dict)]

    # Validate the complete plan before view filtering.
    if isinstance(plan, dict) and all(isinstance(item, dict) for item in plan_items or []):
        fatal, _item_errors = _entity_import_plan_preflight(plan, deep=False)
    else:
        unfiltered_plan = dict(plan) if isinstance(plan, dict) else {}
        unfiltered_plan["items"] = source_items
        fatal, _item_errors = _entity_import_plan_preflight(unfiltered_plan, deep=False)
    if fatal:
        return False

    if import_kwargs is not None:
        source_items = cached_plan_filter_items_for_import_options(
            source_items,
            import_kwargs or {},
            context=context,
        )
        source_items = _maybe_group_cached_items_into_sector_instancers(source_items, import_kwargs)
        source_items = _ensure_cached_sector_group_hierarchy(source_items)
    filtered_items = _filter_cached_plan_items_by_proximity(
        source_items,
        nearby_filter,
        nearby_stats,
    )
    for item in filtered_items:
        kind = str(item.get("kind", "") or "").strip().lower()
        if kind in _CACHED_FULL_ITEM_KINDS:
            continue
        if kind in _CACHED_FULL_PARENT_ITEM_KINDS:
            continue
        if kind:
            return False
    return True


def _filter_cached_plan_items_by_kinds(items, item_kinds):
    if not item_kinds:
        return list(items or [])
    wanted_kinds = {str(kind or "").strip().lower() for kind in item_kinds if str(kind or "").strip()}
    if not wanted_kinds:
        return list(items or [])

    by_id = {}
    for item in items or []:
        item_id = str(item.get("id", "") or "").strip() if isinstance(item, dict) else ""
        if item_id:
            by_id[item_id] = item

    keep = set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id", "") or "").strip()
        kind = str(item.get("kind", "") or "").strip().lower()
        if not item_id or kind not in wanted_kinds:
            continue
        current = item
        while current is not None:
            current_id = str(current.get("id", "") or "").strip()
            if not current_id or current_id in keep:
                break
            keep.add(current_id)
            parent_id = str(current.get("parent_id", "") or "").strip()
            current = by_id.get(parent_id)

    return [item for item in items or [] if isinstance(item, dict) and str(item.get("id", "") or "").strip() in keep]


def _cached_plan_items_by_id(items):
    by_id = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id", "") or "").strip()
        if item_id:
            by_id[item_id] = item
    return by_id


def _cached_plan_parent_chain(item, by_id):
    current = item
    seen = set()
    while isinstance(current, dict):
        parent_id = str(current.get("parent_id", "") or "").strip()
        if not parent_id or parent_id in seen:
            break
        seen.add(parent_id)
        current = by_id.get(parent_id)
        if isinstance(current, dict):
            yield current
        else:
            break


def _cached_plan_nearest_entity_parent(item, by_id):
    for parent in _cached_plan_parent_chain(item, by_id):
        if str(parent.get("kind", "") or "").strip().lower() == "entity":
            return parent
    return None


def _cached_plan_item_matches_regex(item, by_id, regex_pattern):
    if regex_pattern is None:
        return True
    entity_parent = _cached_plan_nearest_entity_parent(item, by_id)
    candidates = (
        [entity_parent.get("name", "")]
        if entity_parent is not None
        else [item.get("name", ""), item.get("repo_path", "")]
    )
    for value in candidates:
        value = str(value or "")
        if value and regex_pattern.search(value):
            return True
    return False


def _cached_plan_item_enabled_by_import_options(item, by_id, kwargs, *, context=None):
    kind = str(item.get("kind", "") or "").strip().lower()
    if kind in _CACHED_FULL_PARENT_ITEM_KINDS:
        return False
    is_proxy_mesh = kind in {"mesh", "component_mesh"} and _cached_plan_item_is_proxy_mesh(item)
    if is_proxy_mesh and _proxy_mesh_filter_active(kwargs):
        return bool(kwargs.get("do_import_ProxyMesh", False))
    if kind in _CACHED_REDCLOTH_ITEM_KINDS:
        return _cloth_resource_enabled_for_import(item.get("repo_path", ""), kwargs, context)
    if kind in {"mesh", "component_mesh", "foliage", "grass"}:
        return bool(kwargs.get("do_import_Mesh", True))
    if kind == "collision":
        return bool(kwargs.get("do_import_Collision", True))
    if kind in {"rigid", "rigid_body"}:
        return bool(kwargs.get("do_import_RigidBody", True))
    if kind in {"point_light", "component_point_light"}:
        return bool(kwargs.get("do_import_PointLight", True))
    if kind in {"spot_light", "component_spot_light"}:
        return bool(kwargs.get("do_import_SpotLight", True))
    if kind in _CACHED_FULL_EMPTY_ITEM_KINDS:
        return bool(kwargs.get("do_import_Entity", True))
    if kind in {"entity_template", "entity_asset"}:
        return bool(kwargs.get("do_import_Entity", True))
    return True


def cached_plan_filter_items_for_import_options(items, kwargs=None, *, context=None):
    source_items = [item for item in items or [] if isinstance(item, dict)]
    kwargs = dict(kwargs or {})
    if not source_items:
        return []

    regex_pattern = None
    if bool(kwargs.get("do_enable_name_filter", False)):
        regex_text = str(kwargs.get("do_name_filter_regex", "") or "")
        if regex_text:
            try:
                regex_pattern = re.compile(regex_text)
            except Exception:
                log.warning("Invalid layer import regex filter: %s", regex_text)
                return []

    by_id = _cached_plan_items_by_id(source_items)
    keep = set()
    for item in source_items:
        item_id = str(item.get("id", "") or "").strip()
        if not item_id:
            continue
        if not _cached_plan_item_enabled_by_import_options(item, by_id, kwargs, context=context):
            continue
        if not _cached_plan_item_matches_regex(item, by_id, regex_pattern):
            continue
        current = item
        while isinstance(current, dict):
            current_id = str(current.get("id", "") or "").strip()
            if not current_id or current_id in keep:
                break
            keep.add(current_id)
            parent_id = str(current.get("parent_id", "") or "").strip()
            current = by_id.get(parent_id)

    return [item for item in source_items if str(item.get("id", "") or "").strip() in keep]


def _import_plan_as_dev_empties(plan, target_collection, kwargs):
    if target_collection is None:
        target_collection = _get_active_collection()
    created = {}
    owner_tag = kwargs.get("_layer_import_owner")

    for item in plan.get("items", []):
        _raise_if_layer_import_cancelled(kwargs)
        obj = _create_linked_empty(item.get("name", "Empty"), target_collection)
        if obj is None:
            continue
        parent_obj = created.get(str(item.get("parent_id", "") or "").strip())
        if parent_obj is not None:
            obj.parent = parent_obj
        _apply_plan_item_transform_for_parent(obj, item, parent_obj)
        _tag_single_object_for_layer(obj, owner_tag)
        try:
            obj["witcher_dev_proxy"] = True
            obj["witcher_dev_kind"] = str(item.get("kind", "") or "")
            repo_path = str(item.get("repo_path", "") or "").strip()
            if repo_path:
                obj["witcher_dev_source_path"] = repo_path
        except Exception:
            pass
        created[item["id"]] = obj
    return len(created)


def _import_cached_plan_redcloth_items(plan, target_collection, kwargs, context=None, loaded_collection=None, errors=None):
    if target_collection is None:
        target_collection = _get_active_collection(context)
    if target_collection is None:
        return 0

    from ..importers import import_entity

    items = [item for item in plan.get("items", []) or [] if isinstance(item, dict)]
    by_id = {
        str(item.get("id", "") or "").strip(): item
        for item in items
        if str(item.get("id", "") or "").strip()
    }
    mode_signature = str(kwargs.get("_layer_import_mode_signature", "") or "").strip()
    if not mode_signature:
        mode_signature = _layer_load_mode_signature(False)
    loaded_item_ids = _cached_plan_loaded_item_ids(loaded_collection or target_collection, mode_signature)
    needed_ids = set()

    for item in items:
        item_id = str(item.get("id", "") or "").strip()
        kind = str(item.get("kind", "") or "").strip().lower()
        repo_path_value = str(item.get("repo_path", "") or "").strip()
        if kind not in _CACHED_REDCLOTH_ITEM_KINDS or not item_id or not repo_path_value:
            continue
        if item_id in loaded_item_ids:
            continue
        current = item
        while current is not None:
            current_id = str(current.get("id", "") or "").strip()
            if not current_id or current_id in needed_ids:
                break
            needed_ids.add(current_id)
            parent_id = str(current.get("parent_id", "") or "").strip()
            current = by_id.get(parent_id)

    created = {}
    owner_tag = kwargs.get("_layer_import_owner")

    def ensure_parent_empty(item_id):
        item_id = str(item_id or "").strip()
        if not item_id or item_id not in needed_ids:
            return None
        existing = created.get(item_id)
        if existing is not None:
            return existing
        item = by_id.get(item_id)
        if item is None:
            return None
        kind = str(item.get("kind", "") or "").strip().lower()
        if kind in _CACHED_REDCLOTH_ITEM_KINDS:
            return None
        parent_obj = ensure_parent_empty(str(item.get("parent_id", "") or "").strip())
        obj = _create_linked_empty(item.get("name", "Entity"), target_collection)
        if obj is None:
            return None
        if parent_obj is not None:
            obj.parent = parent_obj
        _apply_plan_item_transform_for_parent(obj, item, parent_obj)
        _tag_single_object_for_layer(obj, owner_tag)
        _tag_object_tree_for_plan_item(obj, item_id, mode_signature)
        try:
            if kind == "entity":
                _set_redkit_entity_metadata(
                    obj,
                    str(item.get("entity_class", "") or "CEntity"),
                    entity_name=str(item.get("name", "") or ""),
                    template_path=str(item.get("repo_path", "") or ""),
                    action_name=str(item.get("action_name", "") or ""),
                    instance_id=str(item.get("instance_id", "") or ""),
                    entity_guid=str(item.get("entity_guid", "") or ""),
                    entity_id=str(item.get("entity_id", "") or ""),
                )
                if "engine_visible" in item:
                    obj["witcher_entity_instance_visible"] = bool(item["engine_visible"])
                    _tag_object_tree_engine_visibility(obj, item["engine_visible"], kwargs)
            obj["witcher_cached_plan_proxy"] = True
            obj["witcher_cached_plan_kind"] = kind
        except Exception:
            pass
        created[item_id] = obj
        return obj

    imported_count = 0
    for item in items:
        _raise_if_layer_import_cancelled(kwargs)
        item_id = str(item.get("id", "") or "").strip()
        if not item_id or item_id not in needed_ids:
            continue
        kind = str(item.get("kind", "") or "").strip().lower()
        if kind not in _CACHED_REDCLOTH_ITEM_KINDS:
            ensure_parent_empty(item_id)
            continue
        resource = str(item.get("repo_path", "") or "").strip()
        if not resource or item_id in loaded_item_ids:
            continue
        if not _cloth_resource_enabled_for_import(resource, kwargs, context):
            continue
        parent_obj = ensure_parent_empty(str(item.get("parent_id", "") or "").strip())
        try:
            if _is_redapex_resource(resource):
                root_obj, _cloth_meshes = import_entity.import_or_reuse_redapex(
                    resource,
                    repo_file(resource),
                    context=context or bpy.context,
                    loadmods=bool(kwargs.get("loadmods", False)),
                    target_collection=target_collection,
                    **_redapex_import_options(kwargs),
                )
                cloth_arma = root_obj
                cloth_grp = None
            else:
                cloth_arma, cloth_grp, _cloth_meshes = import_entity.import_or_reuse_redcloth(
                    parent_obj,
                    resource,
                    repo_file(resource),
                    import_name="CClothComponent",
                    entity_name=str(item.get("name", "") or Path(resource.replace("/", "\\")).stem),
                    target_collection=target_collection,
                    hide_collision_proxies=bool(kwargs.get("hide_proxy_meshes", False)),
                )
        except Exception as exc:
            resource_label = "redapex" if _is_redapex_resource(resource) else "redcloth"
            log.warning("Problem with cached %s import %s: %s", resource_label, resource, exc)
            if errors is not None:
                errors.append(f"Problem with cached {resource_label} import {resource}: {exc}")
            continue
        failure_reason = _cloth_import_failure_reason(cloth_arma)
        if failure_reason:
            resource_label = "redapex" if _is_redapex_resource(resource) else "redcloth"
            message = f"Problem with cached {resource_label} import {resource}: {failure_reason}"
            log.warning("%s", message)
            if errors is not None:
                errors.append(message)
            continue
        root_obj = cloth_grp if cloth_grp is not None else cloth_arma
        _apply_cached_destruction_metadata(root_obj, item, kwargs)
        _apply_requested_proxy_helper_visibility(root_obj, kwargs)
        if parent_obj is not None:
            root_obj.parent = parent_obj
            _apply_plan_item_transform_as_child(root_obj, item)
        else:
            _apply_plan_item_transform(root_obj, item)
        _tag_object_tree_for_layer_and_plan(
            root_obj,
            owner_tag,
            item_id,
            mode_signature,
        )
        loaded_item_ids.add(item_id)
        imported_count += 1

    return imported_count


def _cached_plan_mesh_enabled(kind, kwargs, item=None):
    if kind in {"mesh", "component_mesh"} and _cached_plan_item_is_proxy_mesh(item) and _proxy_mesh_filter_active(kwargs):
        return bool(kwargs.get("do_import_ProxyMesh", False))
    if kind in {"mesh", "component_mesh", "foliage", "grass"}:
        return bool(kwargs.get("do_import_Mesh", True))
    if kind == "sector_instancer":
        # Grouped instancers were already filtered upstream.
        source_kind = str((item or {}).get("_source_kind", "") or "").strip().lower()
        if source_kind in {"rigid", "rigid_body"}:
            return bool(kwargs.get("do_import_RigidBody", True))
        if source_kind == "collision":
            return bool(kwargs.get("do_import_Collision", True))
        return bool(kwargs.get("do_import_Mesh", True))
    if kind == "collision":
        return bool(kwargs.get("do_import_Collision", True))
    if kind in {"rigid", "rigid_body"}:
        return bool(kwargs.get("do_import_RigidBody", True))
    return False


def _cached_plan_mesh_from_item(item, kind, context=None):
    repo_path_value = str(item.get("repo_path", "") or "").strip()
    if not repo_path_value:
        return None
    block_type = Enums.BlockDataObjectType.Mesh
    if kind == "collision":
        block_type = Enums.BlockDataObjectType.Collision
    elif kind in {"rigid", "rigid_body"}:
        block_type = Enums.BlockDataObjectType.RigidBody

    fbx_uncook_path = None
    if kind in {"foliage", "grass"}:
        fbx_uncook_path = get_W3_FOLIAGE_PATH(context or bpy.context)

    mesh = _new_mesh_path(
        repo_path_value,
        item.get("translation") or False,
        item.get("matrix") or False,
        fbx_uncook_path=fbx_uncook_path,
        uncook_path=item.get("mesh_uncook_path") or None,
        transform=item.get("transform") or False,
        block_data_object_type=block_type,
    )
    if item.get("sector_flags") is not None:
        try:
            mesh.sector_flags = int(item.get("sector_flags"))
        except Exception:
            pass
    if item.get("engine_visible") is not None:
        try:
            mesh.engine_visible = bool(item.get("engine_visible"))
        except Exception:
            pass
    if item.get("drawable_flags") is not None:
        try:
            mesh.drawable_flags = item.get("drawable_flags")
        except Exception:
            pass
    try:
        mesh.is_proxy_mesh = _cached_plan_item_is_proxy_mesh(item)
        mesh.proxy_role = str(item.get("proxy_role", "") or "")
    except Exception:
        pass
    try:
        mesh.import_name = str(item.get("name", "") or "").strip()
    except Exception:
        pass
    embedded_cmesh_chunk_index = _normalize_embedded_cmesh_chunk_index(
        item.get("embedded_cmesh_chunk_index", item.get("mesh_chunk_indices"))
    )
    if embedded_cmesh_chunk_index is not None:
        mesh.embedded_cmesh_chunk_index = embedded_cmesh_chunk_index
    if item.get("cr2w_version") is not None:
        mesh.cr2w_version = _mesh_cr2w_version(mesh, item.get("cr2w_version"))
    if kind in {"foliage", "grass"}:
        mesh.type = "mesh_foliage"
    return mesh


def _cached_plan_light_enabled(kind, kwargs):
    if kind in {"point_light", "component_point_light"}:
        return bool(kwargs.get("do_import_PointLight", True))
    if kind in {"spot_light", "component_spot_light"}:
        return bool(kwargs.get("do_import_SpotLight", True))
    return False


def _cached_plan_float(item, key, default=0.0):
    try:
        return float(item.get(key, default))
    except Exception:
        return float(default)


def _cached_plan_light_color(item):
    color = item.get("color")
    if isinstance(color, dict):
        try:
            return (
                float(color.get("Red", color.get("red", 255.0))) / 255.0,
                float(color.get("Green", color.get("green", 255.0))) / 255.0,
                float(color.get("Blue", color.get("blue", 255.0))) / 255.0,
            )
        except Exception:
            return (1.0, 1.0, 1.0)
    if isinstance(color, (list, tuple)) and len(color) >= 3:
        try:
            values = [float(color[0]), float(color[1]), float(color[2])]
            if max(values) > 1.0:
                values = [value / 255.0 for value in values]
            return tuple(values)
        except Exception:
            return (1.0, 1.0, 1.0)
    return (1.0, 1.0, 1.0)


def _import_cached_plan_light_item(
    item,
    kind,
    target_collection,
    parent_obj,
    owner_tag,
    item_id,
    mode_signature,
):
    component_light = kind in {"component_point_light", "component_spot_light"}
    light_type = "SPOT" if kind == "spot_light" else "POINT"
    name = str(item.get("name", "") or light_type.title())
    light_data = bpy.data.lights.new(name, type=light_type)
    if not component_light:
        brightness = _cached_plan_float(item, "brightness", 1.0)
        default_multiplier = 3.0 if light_type == "SPOT" else 10.0
        light_data.energy = _cached_plan_float(item, "energy", brightness * default_multiplier)
        light_data.color = _cached_plan_light_color(item)

        if item.get("radius") is not None:
            radius_value = _cached_plan_float(item, "radius", 0.0) / 255.0
            light_data.shadow_soft_size = max(0.0, radius_value)

        if light_type == "SPOT":
            light_data.spot_blend = _cached_plan_float(item, "spot_blend", 0.0)
            if item.get("outer_angle") is not None:
                light_data.spot_size = _cached_plan_float(item, "outer_angle", light_data.spot_size)

    light_obj = bpy.data.objects.new(name, light_data)
    target_collection.objects.link(light_obj)
    if parent_obj is not None:
        light_obj.parent = parent_obj
    _apply_plan_item_transform_for_parent(light_obj, item, parent_obj)
    if component_light:
        component_type = str(
            item.get("component_type", "")
            or ("CSpotLightComponent" if kind == "component_spot_light" else "CPointLightComponent")
        )
        configure_entity_light(light_obj, item, component_type, scene=bpy.context.scene)
        if component_type == "CSpotLightComponent":
            orient_red_spot(light_obj)
    elif light_type == "SPOT":
        light_obj.rotation_euler.x += 1.5708
    _tag_single_object_for_layer(light_obj, owner_tag)
    _tag_object_tree_for_plan_item(light_obj, item_id, mode_signature)
    return light_obj


def _import_cached_plan_empty_item(
    item,
    target_collection,
    parent_obj,
    owner_tag,
    item_id,
    mode_signature,
    kwargs=None,
):
    obj = _create_linked_empty(item.get("name", "Component"), target_collection, display_size=0.2)
    if obj is None:
        return None
    if parent_obj is not None:
        obj.parent = parent_obj
    _apply_plan_item_transform_for_parent(obj, item, parent_obj)
    _tag_single_object_for_layer(obj, owner_tag)
    _tag_object_tree_for_plan_item(obj, item_id, mode_signature)
    try:
        kind = str(item.get("kind", "") or "")
        if kind == "component_empty":
            component_type = _component_type_from_plan_item(item, default=str(item.get("component_type", "") or "Component"))
            _set_redkit_component_metadata(
                obj,
                component_type,
                component_name=str(item.get("component_name", "") or ""),
                drawable_flags=item.get("drawable_flags") if "drawable_flags" in item else None,
                engine_visible=item.get("engine_visible") if "engine_visible" in item else None,
                action_name=str(item.get("action_name", "") or ""),
            )
            obj["witcher_meshless_component"] = True
        if kind == "entity_empty":
            obj["witcher_entity_empty_only"] = True
            _set_redkit_entity_metadata(
                obj,
                str(item.get("entity_class", "") or "CEntity"),
                entity_name=str(item.get("name", "") or ""),
                template_path=str(item.get("repo_path", "") or ""),
                action_name=str(item.get("action_name", "") or ""),
                instance_id=str(item.get("instance_id", "") or ""),
                entity_guid=str(item.get("entity_guid", "") or ""),
                entity_id=str(item.get("entity_id", "") or ""),
            )
            if "engine_visible" in item:
                obj["witcher_entity_instance_visible"] = bool(item["engine_visible"])
                _tag_object_tree_engine_visibility(obj, item["engine_visible"], kwargs)
        obj["witcher_cached_plan_kind"] = kind
    except Exception:
        pass
    return obj


# ---------------------------------------------------------------------------
# Sector GN instancer helpers
# ---------------------------------------------------------------------------

def _decompose_sector_3x3(mat3_rows):
    """Decompose a transposed CR2W 3×3 matrix."""
    try:
        from mathutils import Matrix
        r0 = mat3_rows[0]
        r1 = mat3_rows[1]
        r2 = mat3_rows[2]
        m3 = Matrix((
            (float(r0[0]), float(r1[0]), float(r2[0])),
            (float(r0[1]), float(r1[1]), float(r2[1])),
            (float(r0[2]), float(r1[2]), float(r2[2])),
        ))
        _, rot_quat, scale = m3.to_4x4().decompose()
        e = rot_quat.to_euler('XYZ')
        return (e.x, e.y, e.z), (scale.x, scale.y, scale.z)
    except Exception:
        return (0.0, 0.0, 0.0), (1.0, 1.0, 1.0)


def _rebuild_sector_instancer_mesh(instancer_obj, transforms):
    """Rebuild an instancer point cloud from transforms."""
    mesh = instancer_obj.data
    n = len(transforms)
    mesh.clear_geometry()
    if n == 0:
        return
    flat_pos, flat_rot, flat_scale = [], [], []
    for (x, y, z, ex, ey, ez, sx, sy, sz) in transforms:
        flat_pos  += [x, y, z]
        flat_rot  += [ex, ey, ez]
        flat_scale += [sx, sy, sz]
    mesh.vertices.add(n)
    mesh.vertices.foreach_set("co", flat_pos)
    for attr_name, flat_data in (("rot", flat_rot), ("scale", flat_scale)):
        existing = mesh.attributes.get(attr_name)
        if existing is not None:
            mesh.attributes.remove(existing)
        attr = mesh.attributes.new(attr_name, 'FLOAT_VECTOR', 'POINT')
        attr.data.foreach_set("vector", flat_data)
    mesh.update()


def _build_sector_instancer_gn_tree(ng, source_obj):
    """Build the sector Geometry Nodes instancer."""
    nodes = ng.nodes
    links = ng.links
    nodes.clear()

    use_iface = hasattr(ng, "interface") and hasattr(ng.interface, "new_socket")

    def _sock(name, in_out, stype):
        if use_iface:
            ng.interface.new_socket(name=name, in_out=in_out, socket_type=stype)
        else:
            (ng.inputs if in_out == 'INPUT' else ng.outputs).new(stype, name)

    _sock("Geometry", "OUTPUT", "NodeSocketGeometry")
    _sock("Geometry", "INPUT",  "NodeSocketGeometry")

    gin  = nodes.new('NodeGroupInput');  gin.location  = (-800, 0)
    gout = nodes.new('NodeGroupOutput'); gout.location = ( 500, 0)

    na_rot = nodes.new('GeometryNodeInputNamedAttribute')
    na_rot.location = (-600, -100)
    for v in ('FLOAT_VECTOR', 'VECTOR'):
        try: na_rot.data_type = v; break
        except Exception: pass
    try:    na_rot.inputs["Name"].default_value = "rot"
    except Exception: na_rot.inputs[0].default_value = "rot"

    na_scale = nodes.new('GeometryNodeInputNamedAttribute')
    na_scale.location = (-600, -250)
    for v in ('FLOAT_VECTOR', 'VECTOR'):
        try: na_scale.data_type = v; break
        except Exception: pass
    try:    na_scale.inputs["Name"].default_value = "scale"
    except Exception: na_scale.inputs[0].default_value = "scale"

    e2r = None
    for bl_id in ('FunctionNodeEulerToRotation', 'FunctionNodeRotationFromEuler'):
        try: e2r = nodes.new(bl_id); e2r.location = (-350, -100); break
        except Exception: pass

    oi = nodes.new('GeometryNodeObjectInfo')
    oi.location = (-350, -300)
    try:    oi.inputs['Object'].default_value = source_obj
    except Exception: pass
    try:    oi.transform_space = 'ORIGINAL'
    except Exception: pass

    iop = nodes.new('GeometryNodeInstanceOnPoints')
    iop.location = (150, 0)

    links.new(gin.outputs['Geometry'], iop.inputs['Points'])
    links.new(oi.outputs['Geometry'],  iop.inputs['Instance'])
    if e2r is not None:
        links.new(na_rot.outputs[0], e2r.inputs[0])
        try:    links.new(e2r.outputs['Rotation'], iop.inputs['Rotation'])
        except Exception: links.new(e2r.outputs[0], iop.inputs['Rotation'])
    else:
        try: links.new(na_rot.outputs[0], iop.inputs['Rotation'])
        except Exception: pass
    try:    links.new(na_scale.outputs[0], iop.inputs['Scale'])
    except Exception: pass
    links.new(iop.outputs['Instances'], gout.inputs['Geometry'])


def _instancer_group_matches_source(ng, source_obj):
    try:
        for node in ng.nodes:
            if node.bl_idname == 'GeometryNodeObjectInfo':
                return node.inputs['Object'].default_value == source_obj
    except Exception:
        return False
    return False


def _get_or_build_sector_instancer_group(src_root, stem):
    ng_name = str(src_root.get("_si_node_group", "") or "")
    if ng_name:
        ng = bpy.data.node_groups.get(ng_name)
        if ng is not None and _instancer_group_matches_source(ng, src_root):
            return ng
    ng = bpy.data.node_groups.new(f"SectorInstancer_{stem}", 'GeometryNodeTree')
    _build_sector_instancer_gn_tree(ng, src_root)
    src_root["_si_node_group"] = ng.name
    return ng


_SECTOR_SOURCE_MARKER_PROP = "_sector_source_repo"
_SECTOR_SOURCES_COLLECTION_NAME = "__sector_sources__"
_SECTOR_SOURCE_CACHE = {
    "collection_key": None,
    "dirty": True,
    "sources": {},
}
_SECTOR_MISSING_NXS_CACHE = set()


def _id_key(data_block):
    if data_block is None:
        return None
    try:
        return int(data_block.as_pointer())
    except Exception:
        return id(data_block)


def _get_sector_sources_collection(context=None):
    """Return the persistent hidden sector-source collection."""
    existing = bpy.data.collections.get(_SECTOR_SOURCES_COLLECTION_NAME)
    if existing is not None:
        return existing
    coll = bpy.data.collections.new(_SECTOR_SOURCES_COLLECTION_NAME)
    coll.hide_viewport = True
    coll.hide_render = True
    scene = _get_scene(context)
    if scene is not None:
        try:
            scene.collection.children.link(coll)
        except Exception:
            pass
    return coll


def _get_cached_sector_source(marker):
    marker = str(marker or "").strip()
    if not marker:
        return None
    sources_coll = _get_sector_sources_collection()
    collection_key = _id_key(sources_coll)
    if _SECTOR_SOURCE_CACHE.get("dirty") or _SECTOR_SOURCE_CACHE.get("collection_key") != collection_key:
        sources = {}
        for obj in list(getattr(sources_coll, "all_objects", []) or []):
            try:
                key = str(obj.get(_SECTOR_SOURCE_MARKER_PROP, "") or "").strip()
                if key:
                    sources[key] = obj
            except Exception:
                continue
        _SECTOR_SOURCE_CACHE["collection_key"] = collection_key
        _SECTOR_SOURCE_CACHE["dirty"] = False
        _SECTOR_SOURCE_CACHE["sources"] = sources
    obj = (_SECTOR_SOURCE_CACHE.get("sources") or {}).get(marker)
    if obj is None:
        return None
    try:
        if obj.name in bpy.data.objects:
            return obj
    except Exception:
        pass
    _SECTOR_SOURCE_CACHE["dirty"] = True
    return None


def _remember_sector_source(marker, source):
    marker = str(marker or "").strip()
    if not marker or source is None:
        return
    _SECTOR_SOURCE_CACHE.setdefault("sources", {})[marker] = source
    _SECTOR_SOURCE_CACHE["collection_key"] = _id_key(_get_sector_sources_collection())
    _SECTOR_SOURCE_CACHE["dirty"] = False


def _nxs_missing_key(path):
    return os.path.normcase(os.path.normpath(str(path or "")))


def _is_missing_nxs(path):
    key = _nxs_missing_key(path)
    return bool(key and key in _SECTOR_MISSING_NXS_CACHE)


def _remember_missing_nxs(path):
    key = _nxs_missing_key(path)
    if key:
        _SECTOR_MISSING_NXS_CACHE.add(key)


def _is_missing_file_error(exc):
    if isinstance(exc, FileNotFoundError):
        return True
    try:
        if getattr(exc, "errno", None) == 2:
            return True
    except Exception:
        pass
    text = str(exc or "").lower()
    return "no such file or directory" in text or "[errno 2]" in text


def _note_missing_nxs_once(path, label):
    if _is_missing_nxs(path):
        return
    _remember_missing_nxs(path)
    log.warning("%s missing NXS skipped %s", label, path)


def _apply_sector_collision_transform_and_tags(wrapper, mesh, parent_transform, kwargs):
    if wrapper is None:
        return None
    if parent_transform:
        wrapper.parent = parent_transform

    # Apply rotation (same column-transposed pattern as import_single_mesh).
    mat_src = getattr(mesh, "matrix", None)
    if mat_src:
        try:
            mat = Matrix()
            mat[0][0], mat[0][1], mat[0][2] = mat_src[0][0], mat_src[1][0], mat_src[2][0]
            mat[1][0], mat[1][1], mat[1][2] = mat_src[0][1], mat_src[1][1], mat_src[2][1]
            mat[2][0], mat[2][1], mat[2][2] = mat_src[0][2], mat_src[1][2], mat_src[2][2]
            wrapper.matrix_world = wrapper.matrix_world @ mat
        except Exception:
            pass

    translation = _extract_vector_position(getattr(mesh, "translation", None))
    if translation is not None:
        wrapper.location[0] = translation[0]
        wrapper.location[1] = translation[1]
        wrapper.location[2] = translation[2]

    _tag_object_tree_for_layer_and_plan(
        wrapper,
        kwargs.get("_layer_import_owner"),
        kwargs.get("_layer_import_plan_item_id"),
        kwargs.get("_layer_import_plan_mode"),
    )
    for obj in [wrapper] + list(getattr(wrapper, "children_recursive", []) or []):
        try:
            obj["witcher_layer_visibility_kind"] = "collision"
        except Exception:
            pass
    return wrapper


def _import_sector_w2mesh_collision(mesh, errors, parent_transform, **kwargs):
    """Import collision-only geometry for a CSectorData collision record backed by .w2mesh."""
    mesh_path = str(getattr(mesh, "meshName", "") or "").strip()
    if not mesh_path or mesh_path == "0":
        return None

    try:
        from .import_nxs import create_from_nxs
        from ..CR2W.common_blender import get_collision_for_mesh_with_poses
    except ImportError as exc:
        log.warning("sector w2mesh collision import: missing module: %s", exc)
        return None

    collision_root_key = f"{mesh_path}#collision"
    existing = check_if_empty_already_in_scene(
        collision_root_key,
        fast_static_clone=bool(kwargs.get("_cached_plan_fast_static_clone", False)),
    )
    if existing:
        return _apply_sector_collision_transform_and_tags(existing, mesh, parent_transform, kwargs)

    collision_path, shape_items = get_collision_for_mesh_with_poses(mesh_path)
    if not collision_path:
        log.debug("No collision cache item found for mesh collision record: %s", mesh_path)
        return None
    if _is_missing_nxs(collision_path):
        return None
    if not os.path.exists(collision_path):
        _note_missing_nxs_once(collision_path, "Mesh collision")
        return None

    try:
        nxs_objects = create_from_nxs(collision_path, shape_items=shape_items)
    except Exception as exc:
        if _is_missing_file_error(exc):
            _note_missing_nxs_once(collision_path, "Mesh collision")
            return None
        log.warning("Mesh collision NXS import failed %s: %s", collision_path, exc)
        if errors is not None:
            errors.append(f"Mesh collision NXS import failed {mesh_path}: {exc}")
        return None

    if not nxs_objects:
        return None

    bpy.ops.object.empty_add(type="PLAIN_AXES", radius=1)
    wrapper = bpy.context.object
    stem = Path(mesh_path.replace("\\", "/")).stem or "Collision"
    wrapper.name = stem
    wrapper["repo_path"] = collision_root_key
    wrapper["witcher_collision_source_repo_path"] = mesh_path
    wrapper["witcher_layer_visibility_kind"] = "collision"
    wrapper.show_in_front = False

    for obj in nxs_objects:
        if obj is not None:
            obj.parent = wrapper
            try:
                obj["witcher_layer_visibility_kind"] = "collision"
                obj.show_in_front = False
            except Exception:
                pass

    _record_duplicate_root(wrapper, subtree=[wrapper] + [o for o in nxs_objects if o is not None])
    return _apply_sector_collision_transform_and_tags(wrapper, mesh, parent_transform, kwargs)


def _import_sector_collision_from_cache(mesh, errors, parent_transform, **kwargs):
    """Import sector collision blocks as collision-only geometry."""
    collision_path = str(getattr(mesh, "meshName", "") or "").strip()
    if not collision_path or collision_path == "0":
        return None
    if _sector_collision_path_is_visual_mesh(collision_path):
        return _import_sector_w2mesh_collision(mesh, errors, parent_transform, **kwargs)

    try:
        from .import_nxs import create_from_nxs
        from ..CR2W.witcher_cache.CollisionCache.CollisionManager import CollisionManager
    except ImportError as exc:
        log.warning("sector collision import: missing module: %s", exc)
        return None

    manager = CollisionManager.Get()

    # Prefer exact key match, fall back to case-insensitive
    raw = manager.Items.get(collision_path)
    item = None
    if raw:
        item = raw[0] if isinstance(raw, list) else raw
    if item is None:
        collision_lower = collision_path.lower()
        for key, val in manager.Items.items():
            if key.lower() == collision_lower:
                item = val[0] if isinstance(val, list) else val
                break

    if item is None:
        log.debug("Collision cache item not found for sector block: %s", collision_path)
        return None

    existing = check_if_empty_already_in_scene(
        collision_path,
        fast_static_clone=bool(kwargs.get("_cached_plan_fast_static_clone", False)),
    )
    if existing:
        return _apply_sector_collision_transform_and_tags(existing, mesh, parent_transform, kwargs)

    uncook = get_uncook_path(bpy.context)
    if not uncook:
        log.warning("No uncook path for collision extraction: %s", collision_path)
        return None

    ext = item.Extension or ".nxs"
    out_path = os.path.join(uncook, collision_path.replace("/", "\\"))
    if not out_path.lower().endswith(ext.lower()):
        out_path = os.path.splitext(out_path)[0] + ext

    if not os.path.exists(out_path):
        try:
            item.extract_to_file(out_path)
        except Exception as exc:
            if _is_missing_file_error(exc):
                _note_missing_nxs_once(out_path, "Collision extraction")
                return None
            log.warning("Collision extraction failed %s: %s", collision_path, exc)
            if errors is not None:
                errors.append(f"Collision extraction failed {collision_path}: {exc}")
            return None

    if _is_missing_nxs(out_path):
        return None
    try:
        try:
            shape_items = item.get_shapes_with_data()
        except Exception:
            shape_items = []
        nxs_objects = create_from_nxs(out_path, shape_items=shape_items or None)
    except Exception as exc:
        if _is_missing_file_error(exc):
            _note_missing_nxs_once(out_path, "Collision")
            return None
        log.warning("NXS import failed %s: %s", out_path, exc)
        if errors is not None:
            errors.append(f"NXS import failed {out_path}: {exc}")
        return None

    if not nxs_objects:
        return None

    bpy.ops.object.empty_add(type="PLAIN_AXES", radius=1)
    wrapper = bpy.context.object
    stem = Path(collision_path.replace("\\", "/")).stem or "Collision"
    wrapper.name = stem
    wrapper["repo_path"] = collision_path
    wrapper["witcher_layer_visibility_kind"] = "collision"
    wrapper.show_in_front = False

    for obj in nxs_objects:
        if obj is not None:
            obj.parent = wrapper
            try:
                obj.show_in_front = False
            except Exception:
                pass

    _record_duplicate_root(wrapper, subtree=[wrapper] + [o for o in nxs_objects if o is not None])
    return _apply_sector_collision_transform_and_tags(wrapper, mesh, parent_transform, kwargs)


def _join_nxs_objects_to_single_mesh(nxs_objects, name):
    """Merge all mesh objects returned by create_from_nxs into one mesh object.

    NXS files can contain multiple shapes (convex hulls, tri-meshes). For GN instancing
    all shapes must live in one source object so Object Info emits all of them together.
    Uses bmesh.from_mesh which appends (merges) geometry into an existing BMesh.
    """
    import bmesh as _bmesh
    mesh_objs = [o for o in (nxs_objects or []) if o is not None and o.type == 'MESH']
    if not mesh_objs:
        return None

    if len(mesh_objs) == 1:
        mesh_objs[0].name = name
        return mesh_objs[0]

    combined_mesh = bpy.data.meshes.new(name)
    bm = _bmesh.new()
    try:
        for obj in mesh_objs:
            mesh_copy = obj.data.copy()
            # Embed the object's own local transform so shapes align correctly
            mesh_copy.transform(obj.matrix_local)
            bm.from_mesh(mesh_copy)
            bpy.data.meshes.remove(mesh_copy)
        bm.to_mesh(combined_mesh)
    finally:
        bm.free()

    combined_obj = bpy.data.objects.new(name, combined_mesh)
    combined_obj.show_in_front = False
    # Remove the now-redundant individual objects
    for obj in mesh_objs:
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
        except Exception:
            pass
    return combined_obj


def _get_or_import_sector_nxs_source_mesh(repo_path, kwargs, errors, mode_signature, item_id):
    """Return a single mesh object suitable as GN source for an NXS collision instancer.

    For standalone collision-cache paths, look up repo_path directly in the
    CollisionManager. For CSectorData collision records that point at .w2mesh,
    resolve the mesh's collision cache/NXS entry first. The returned object is a
    single joined mesh stored in the __sector_sources__ collection.
    """
    repo_path = str(repo_path or "").strip()
    if not repo_path:
        return None
    is_mesh_collision = _sector_collision_path_is_visual_mesh(repo_path)
    marker = f"{repo_path}#collision" if is_mesh_collision else repo_path

    cached_source = _get_cached_sector_source(marker)
    if cached_source is not None:
        return cached_source

    try:
        from .import_nxs import create_from_nxs
        if is_mesh_collision:
            from ..CR2W.common_blender import get_collision_for_mesh_with_poses
        else:
            from ..CR2W.witcher_cache.CollisionCache.CollisionManager import CollisionManager
    except ImportError as exc:
        log.warning("sector nxs source: missing module: %s", exc)
        return None

    shape_items = None
    if is_mesh_collision:
        out_path, shape_items = get_collision_for_mesh_with_poses(repo_path)
        if not out_path:
            log.debug("NXS source not found for mesh collision: %s", repo_path)
            return None
        if _is_missing_nxs(out_path):
            return None
        if not os.path.exists(out_path):
            _note_missing_nxs_once(out_path, "NXS source")
            return None
    else:
        manager = CollisionManager.Get()
        raw = manager.Items.get(repo_path)
        item = None
        if raw:
            item = raw[0] if isinstance(raw, list) else raw
        if item is None:
            path_lower = repo_path.lower()
            for key, val in manager.Items.items():
                if key.lower() == path_lower:
                    item = val[0] if isinstance(val, list) else val
                    break

        if item is None:
            log.debug("NXS source not found in collision cache: %s", repo_path)
            return None

        uncook = get_uncook_path(bpy.context)
        if not uncook:
            return None

        ext = item.Extension or ".nxs"
        out_path = os.path.join(uncook, repo_path.replace("/", "\\"))
        if not out_path.lower().endswith(ext.lower()):
            out_path = os.path.splitext(out_path)[0] + ext

        if not os.path.exists(out_path):
            try:
                item.extract_to_file(out_path)
            except Exception as exc:
                if _is_missing_file_error(exc):
                    _note_missing_nxs_once(out_path, "NXS extraction")
                    return None
                log.warning("NXS extraction failed %s: %s", repo_path, exc)
                if errors is not None:
                    errors.append(f"NXS extraction failed {repo_path}: {exc}")
                return None

    if _is_missing_nxs(out_path):
        return None
    try:
        nxs_objects = create_from_nxs(out_path, shape_items=shape_items)
    except Exception as exc:
        if _is_missing_file_error(exc):
            _note_missing_nxs_once(out_path, "NXS source")
            return None
        log.warning("NXS import failed %s: %s", out_path, exc)
        if errors is not None:
            errors.append(f"NXS import failed {out_path}: {exc}")
        return None

    if not nxs_objects:
        return None

    stem = Path(repo_path.replace("\\", "/")).stem or "ColSrc"
    source = _join_nxs_objects_to_single_mesh(nxs_objects, f"si_{stem}_src")
    if source is None:
        return None

    source[_SECTOR_SOURCE_MARKER_PROP] = marker
    source["_is_sector_source"] = True
    source["repo_path"] = marker
    if is_mesh_collision:
        source["witcher_collision_source_repo_path"] = repo_path
    source.hide_viewport = True
    source.hide_render = True
    source.show_in_front = False

    for c in list(source.users_collection):
        try:
            c.objects.unlink(source)
        except Exception:
            pass
    sources_coll = _get_sector_sources_collection()
    try:
        sources_coll.objects.link(source)
    except Exception:
        pass
    _remember_sector_source(marker, source)
    return source


def _sector_source_lod_level(obj):
    settings = getattr(obj, "witcherui_MeshSettings", None)
    if settings is not None:
        try:
            value = settings.get("source_lod_level", None)
        except Exception:
            try:
                value = settings["source_lod_level"]
            except Exception:
                value = None
        if value is not None:
            try:
                return int(value)
            except Exception:
                pass
    name = str(getattr(obj, "name", "") or "").lower()
    match = re.search(r"(?:^|[_.-])lod[_.-]?(\d+)(?:$|[_.-])", name)
    return int(match.group(1)) if match else None


def _primary_sector_source_meshes(mesh_objects):
    candidates = list(mesh_objects or [])
    known = [(obj, _sector_source_lod_level(obj)) for obj in candidates]
    levels = [level for _obj, level in known if level is not None]
    if not levels:
        return candidates
    primary_level = min(levels)
    # Keep unknown chunks with the primary LOD.
    return [obj for obj, level in known if level is None or level == primary_level]


def _object_matrix_relative_to_ancestor(obj, ancestor=None):
    """Compose authoritative basis transforms without requiring depsgraph evaluation."""
    current = obj
    result = Matrix.Identity(4)
    seen = set()
    while current is not None and current is not ancestor:
        key = _object_identity(current)
        if key in seen:
            break
        seen.add(key)
        try:
            local = current.matrix_parent_inverse @ current.matrix_basis
        except Exception:
            local = current.matrix_basis.copy()
        result = local @ result
        current = getattr(current, "parent", None)
    if ancestor is None or current is ancestor:
        return result
    try:
        return ancestor.matrix_world.inverted_safe() @ obj.matrix_world
    except Exception:
        return result


def _merge_sector_source_meshes(mesh_objects, name, parent_root=None):
    """Merge visual mesh chunks into one GN source while preserving materials and UVs."""
    import bmesh as _bmesh

    combined_mesh = bpy.data.meshes.new(name)
    combined_obj = bpy.data.objects.new(name, combined_mesh)
    materials = []
    material_indices = {}
    bm = _bmesh.new()
    try:
        for obj in mesh_objects:
            source_mesh = obj.data
            mesh_copy = source_mesh.copy()
            try:
                relative_matrix = _object_matrix_relative_to_ancestor(obj, parent_root)
                mesh_copy.transform(relative_matrix)
                slot_remap = {}
                for slot_index, material in enumerate(getattr(source_mesh, "materials", []) or []):
                    if material is None:
                        continue
                    try:
                        material_key = (slot_index, int(material.as_pointer()))
                    except Exception:
                        material_key = (slot_index, id(material))
                    combined_index = material_indices.get(material_key)
                    if combined_index is None:
                        combined_index = len(materials)
                        materials.append(material)
                        material_indices[material_key] = combined_index
                    slot_remap[slot_index] = combined_index
                for polygon in mesh_copy.polygons:
                    polygon.material_index = slot_remap.get(int(polygon.material_index), 0)
                bm.from_mesh(mesh_copy)
            finally:
                bpy.data.meshes.remove(mesh_copy)
        bm.to_mesh(combined_mesh)
    except Exception:
        bpy.data.objects.remove(combined_obj, do_unlink=True)
        if combined_mesh.users == 0:
            bpy.data.meshes.remove(combined_mesh)
        raise
    finally:
        bm.free()

    for material in materials:
        combined_mesh.materials.append(material)
    combined_obj["witcher_expected_material_count"] = len(materials)
    combined_mesh.update()
    import_mesh._ensure_raw_vertex_color(combined_obj)
    combined_obj.show_in_front = False
    return combined_obj


def _pick_best_sector_source_mesh(new_objects, parent_root):
    """Merge a freshly imported LOD0 hierarchy into one GN source mesh."""
    candidates = [o for o in new_objects if o is not None and o.type == 'MESH' and getattr(o, "data", None)]
    candidates = _primary_sector_source_meshes(candidates)
    if not candidates:
        for o in new_objects:
            if o is None:
                continue
            try:
                bpy.data.objects.remove(o, do_unlink=True)
            except Exception:
                pass
        return None

    stem = Path(str(getattr(parent_root, "name", "") or "SectorSource")).stem
    best = _merge_sector_source_meshes(candidates, f"{stem}_merged", parent_root=parent_root)
    for o in list(new_objects):
        if o is None or o is best:
            continue
        try:
            data = o.data if getattr(o, "type", "") == 'MESH' else None
        except Exception:
            data = None
        try:
            bpy.data.objects.remove(o, do_unlink=True)
        except Exception:
            continue
        if data is not None and data.users == 0:
            try:
                bpy.data.meshes.remove(data)
            except Exception:
                pass

    best.parent = None
    try:
        best.matrix_world = Matrix.Identity(4)
        best.matrix_local = Matrix.Identity(4)
        best.matrix_basis = Matrix.Identity(4)
    except Exception:
        pass
    best.location = (0.0, 0.0, 0.0)
    best.rotation_euler = (0.0, 0.0, 0.0)
    best.scale = (1.0, 1.0, 1.0)
    return best


def _get_or_import_sector_source_mesh(repo_path, target_collection, kwargs, errors, mode_signature, item_id, cr2w_version=999, mesh_uncook_path=None):
    """Return the cached or imported LOD0 GN source for ``repo_path``."""
    repo_path = str(repo_path or "").strip()
    if not repo_path:
        return None
    marker = f"{repo_path}#visual_lod0_v2"

    cached_source = _get_cached_sector_source(marker)
    if cached_source is not None:
        import_mesh._ensure_raw_vertex_color(cached_source)
        if not import_mesh.witcher_mesh_materials_ready(cached_source):
            resolved = _resolve_deferred_mesh_path(repo_path, repo_path)
            if kwargs.get("defer_mesh_materials"):
                queue_deferred_mesh_materials(resolved or repo_path, None, [cached_source], repo_path=repo_path)
            elif resolved:
                import_mesh.import_mesh_materials(resolved, [cached_source])
        return cached_source

    src_mesh_data = _new_mesh_path(
        repo_path,
        False, False,
        uncook_path=mesh_uncook_path or kwargs.get("mesh_uncook_path"),
        fbx_uncook_path=kwargs.get("mesh_fbx_uncook_path"),
        cr2w_version=cr2w_version,
    )
    mesh_kwargs = dict(kwargs)
    mesh_kwargs.pop("_cached_plan_fast_static_clone", None)
    # Remove positional-equivalent kwargs so they don't collide with the explicit args below
    mesh_kwargs.pop("keep_lod_meshes", None)
    mesh_kwargs.pop("keep_empty_lods", None)
    mesh_kwargs["_layer_import_plan_item_id"] = (item_id or "") + "_src"
    mesh_kwargs["_layer_import_plan_mode"] = mode_signature

    try:
        wrapper = import_single_mesh(
            src_mesh_data,
            errors,
            None,
            keep_lod_meshes=False,
            **mesh_kwargs,
        )
    except Exception as exc:
        log.warning("sector_instancer source import failed %s: %s", repo_path, exc)
        if errors is not None:
            errors.append(f"sector_instancer source failed {repo_path}: {exc}")
        return None

    # Imported LODs are children of the returned wrapper.
    new_objects = list(_iter_object_tree(wrapper) or [])
    imported_names = [o.name for o in new_objects if getattr(o, "type", "") == 'MESH']
    resolved_source_path = next((
        str(o.get("witcher_resolved_mesh_path", "") or "")
        for o in new_objects
        if getattr(o, "type", "") == 'MESH' and o.get("witcher_resolved_mesh_path")
    ), repo_path)

    # Preserve deferred materials when the imported objects are merged away.
    source = _pick_best_sector_source_mesh(new_objects, wrapper)
    if source is None:
        return None

    stem = Path(repo_path.replace("\\", "/")).stem or "Source"
    source.name = f"si_{stem}_src"
    replace_deferred_queue_objects(imported_names, source.name)
    import_mesh._ensure_raw_vertex_color(source)
    if mesh_kwargs.get("defer_mesh_materials"):
        queue_deferred_mesh_materials(resolved_source_path, None, [source], repo_path=repo_path)
    source[_SECTOR_SOURCE_MARKER_PROP] = marker
    source["_is_sector_source"] = True
    source["repo_path"] = repo_path
    source.hide_viewport = True
    source.hide_render = True
    source.show_in_front = False

    for c in list(source.users_collection):
        try:
            c.objects.unlink(source)
        except Exception:
            pass
    # Keep shared sources outside per-layer cleanup.
    sources_coll = _get_sector_sources_collection()
    try:
        sources_coll.objects.link(source)
    except Exception:
        pass
    _remember_sector_source(marker, source)
    return source


def _import_cached_plan_sector_instancer_item(
    item,
    target_collection,
    parent_obj,
    owner_tag,
    item_id,
    mode_signature,
    kwargs,
    errors,
    context=None,
):
    """Import a sector instancer with a hidden source mesh."""
    repo_path = str(item.get("repo_path", "") or "").strip()
    if not repo_path:
        return None
    sector_transforms = item.get("sector_transforms") or []
    if not sector_transforms:
        return None

    stem = Path(repo_path.replace("\\", "/")).stem or "Mesh"
    source_kind = str(item.get("_source_kind", "") or "mesh").strip().lower()

    if source_kind == "collision":
        src_root = _get_or_import_sector_nxs_source_mesh(
            repo_path, kwargs, errors, mode_signature, item_id,
        )
    else:
        src_root = _get_or_import_sector_source_mesh(
            repo_path,
            target_collection,
            kwargs,
            errors,
            mode_signature,
            item_id,
            cr2w_version=item.get("cr2w_version", 999),
            mesh_uncook_path=item.get("mesh_uncook_path") or None,
        )
    if src_root is None:
        return None

    all_t = []
    for t_entry in sector_transforms:
        if not isinstance(t_entry, dict):
            continue
        tx = t_entry.get("t") or [0.0, 0.0, 0.0]
        mx = t_entry.get("m")
        (ex, ey, ez), (sx, sy, sz) = _decompose_sector_3x3(mx) if mx else ((0.0, 0.0, 0.0), (1.0, 1.0, 1.0))
        try:
            all_t.append((float(tx[0]), float(tx[1]), float(tx[2]), ex, ey, ez, sx, sy, sz))
        except Exception:
            continue
    if not all_t:
        return None

    safe_name = f"si_{stem}"
    inst_mesh = bpy.data.meshes.new(safe_name)
    inst_obj  = bpy.data.objects.new(safe_name, inst_mesh)
    inst_obj.show_in_front = False
    if target_collection is not None:
        target_collection.objects.link(inst_obj)
    if parent_obj is not None:
        inst_obj.parent = parent_obj
    inst_obj["_is_sector_instancer"] = True
    inst_obj["repo_path"] = repo_path
    inst_obj["witcher_layer_visibility_kind"] = source_kind
    if item.get("sector_visible") is not None:
        inst_obj["witcher_layer_engine_visible"] = bool(item.get("sector_visible"))
    elif item.get("sector_visibility_key"):
        inst_obj["witcher_layer_engine_visible"] = str(item.get("sector_visibility_key")).lower() != "hidden"
    if (
        not bool(inst_obj.get("witcher_layer_engine_visible", True))
        and _hide_engine_hidden_meshes_enabled(kwargs, context)
    ):
        inst_obj.hide_viewport = True
        inst_obj.hide_render = True
    if item.get("sector_flags") is not None:
        try:
            inst_obj["witcher_sector_flags"] = int(item.get("sector_flags"))
        except Exception:
            pass
    _tag_single_object_for_layer(inst_obj, owner_tag)
    _tag_object_tree_for_plan_item(inst_obj, item_id, mode_signature)

    _rebuild_sector_instancer_mesh(inst_obj, all_t)

    mod = inst_obj.modifiers.new("SectorInstancer", 'NODES')
    mod.node_group = _get_or_build_sector_instancer_group(src_root, stem)

    log.info(
        "sector_instancer: %s -> %d placements (source=%s)",
        stem, len(all_t), src_root.name,
    )
    return inst_obj


def _snapshot_blender_objects():
    try:
        return {_object_identity(obj): obj for obj in list(bpy.data.objects)}
    except Exception:
        return {}


def _rollback_blender_objects_created_after(before_snapshot):
    after = _snapshot_blender_objects()
    created = [obj for key, obj in after.items() if key not in before_snapshot]
    removed = 0
    for obj in reversed(created):
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
            removed += 1
        except Exception:
            log.warning(
                "Failed to roll back entity import object '%s'.",
                getattr(obj, "name", "<unknown>"),
                exc_info=True,
            )
    return removed


def _materialize_entity_asset_plan_item(
    item,
    target_collection,
    parent_obj,
    owner_tag,
    item_id,
    mode_signature,
    kwargs,
    errors,
    import_entity,
    *,
    context=None,
    shared_armature=None,
    before_snapshot=None,
    snapshot_out=None,
):
    entity_data = item.get("entity_data")
    source_path = str(item.get("source_path", "") or item.get("repo_path", "") or "").strip()
    if not source_path:
        if not isinstance(entity_data, dict):
            errors.append(f"Entity plan item {item_id} has no source path")
            return None
        source_path = f"{str(item.get('name', '') or entity_data.get('name', '') or 'embedded_entity')}.w2ent"

    entity_override = None
    if entity_data is not None:
        if not isinstance(entity_data, dict):
            errors.append(f"Entity plan item {item_id} has invalid embedded entity_data")
            return None
        try:
            entity_override = import_entity.w3_types.Entity.from_json(copy.deepcopy(entity_data))
            override_error = _apply_entity_instance_overrides(
                entity_override,
                item.get("instance_overrides"),
            )
            if override_error:
                errors.append(f"Entity plan item {item_id} has unsupported overrides: {override_error}")
                return None
        except Exception as exc:
            errors.append(f"Could not restore embedded entity data for {source_path}: {exc}")
            return None

    mesh_import_settings = kwargs.get("mesh_import_settings")
    defer_materials = mesh_import_settings is None and bool(kwargs.get("defer_mesh_materials", False))
    if defer_materials:
        mesh_import_settings = {"build_material_nodes": False, "import_morphs": False}

    before = before_snapshot if before_snapshot is not None else _snapshot_blender_objects()
    try:
        if target_collection is not None:
            _activate_collection(context or bpy.context, target_collection)
        component_import_options = {
            option_name: bool(kwargs[option_name])
            for option_name in (
                "do_import_Mesh",
                "do_import_ProxyMesh",
                "do_import_Redcloth",
                "do_import_Redapex",
                "do_import_PointLight",
                "do_import_SpotLight",
            )
            if option_name in kwargs
        }
        returned = import_entity._materialize_entity_asset(
            source_path,
            load_face_poses=bool(kwargs.get("load_face_poses", False)),
            import_apperance=int(kwargs.get("import_apperance", kwargs.get("import_appearance", 0)) or 0),
            parent_transform=parent_obj if parent_obj is not None else kwargs.get("parent_transform"),
            selected_appearance_name=str(
                item.get("selected_appearance", "")
                or kwargs.get("selected_appearance_name", "")
                or ""
            ),
            mesh_import_settings=mesh_import_settings,
            entity_namespace=str(item.get("entity_namespace", "") or kwargs.get("entity_namespace", "") or ""),
            entity_override=entity_override,
            component_import_options=component_import_options or None,
            load_appearance_equipment=bool(kwargs.get("load_appearance_equipment", False)),
            existing_root_skeleton=shared_armature,
            entity_overlay=bool(item.get("entity_asset_overlay", False)),
        )
    except Exception as exc:
        _rollback_blender_objects_created_after(before)
        message = f"Problem with entity import {source_path}: {exc}"
        log.warning("%s", message)
        errors.append(message)
        return None

    after = _snapshot_blender_objects()
    created_objects = [obj for key, obj in after.items() if key not in before]
    created_ids = set(after).difference(before)
    if defer_materials and created_objects:
        try:
            queue_deferred_materials_for_objects(created_objects)
        except Exception:
            log.warning("Could not queue deferred materials for %s", source_path, exc_info=True)
    returned_armature = getattr(returned, "main_armature", None)
    if returned_armature is None and str(getattr(returned, "type", "") or "") == "ARMATURE":
        returned_armature = returned
    if shared_armature is not None:
        returned_armature = shared_armature
    root_obj = None
    if not (bool(item.get("entity_asset_overlay", False)) and shared_armature is not None):
        root_obj = getattr(returned, "main_object", None) or getattr(returned, "root_object", None)
        if root_obj is None and returned is not None and hasattr(returned, "name"):
            root_obj = returned
    while root_obj is not None:
        parent = getattr(root_obj, "parent", None)
        if parent is None or _object_identity(parent) not in created_ids:
            break
        root_obj = parent
    if root_obj is None and created_objects:
        root_obj = next(
            (
                obj for obj in created_objects
                if getattr(obj, "parent", None) is None
                or _object_identity(getattr(obj, "parent", None)) not in created_ids
            ),
            created_objects[0],
        )
    if root_obj is None and parent_obj is not None and entity_override is not None:
        # Preserve materialization state when filtering removes every component.
        root_obj = _create_linked_empty(
            f"{str(item.get('name', '') or 'Entity')} [filtered]",
            target_collection,
        )
        if root_obj is not None:
            root_obj.parent = parent_obj
            created_objects.append(root_obj)
            after[_object_identity(root_obj)] = root_obj
    for obj in created_objects:
        _tag_single_object_for_layer(obj, owner_tag)
        _tag_object_tree_for_plan_item(obj, item_id, mode_signature)
    if root_obj is not None:
        _tag_object_tree_for_layer_and_plan(root_obj, owner_tag, item_id, mode_signature)
    if root_obj is None:
        errors.append(f"Entity import produced no Blender objects: {source_path}")
    execution_results = kwargs.get("_entity_import_execution_results")
    if isinstance(execution_results, dict):
        execution_results[item_id] = {
            "root_object": root_obj,
            "main_armature": returned_armature,
            "created_objects": created_objects,
        }
    if snapshot_out is not None:
        snapshot_out["after"] = after
    return root_obj


def _find_entity_asset_armature(root_obj):
    current = root_obj
    seen = set()
    while current is not None and _object_identity(current) not in seen:
        seen.add(_object_identity(current))
        if str(getattr(current, "type", "") or "") == "ARMATURE":
            return current
        current = getattr(current, "parent", None)

    pending = [root_obj] if root_obj is not None else []
    seen.clear()
    while pending:
        current = pending.pop()
        identity = _object_identity(current)
        if identity in seen:
            continue
        seen.add(identity)
        if str(getattr(current, "type", "") or "") == "ARMATURE":
            return current
        pending.extend(list(getattr(current, "children", None) or ()))
    return None


_ENTITY_CLONE_KEY_PROP = "witcher_entity_clone_key"
_ENTITY_CLONE_SOURCE_NAMES = {}


def _entity_clone_source_lookup(clone_key):
    name = _ENTITY_CLONE_SOURCE_NAMES.get(clone_key)
    if not name:
        return None
    obj = bpy.data.objects.get(name)
    if obj is None or str(obj.get(_ENTITY_CLONE_KEY_PROP, "")) != clone_key:
        _ENTITY_CLONE_SOURCE_NAMES.pop(clone_key, None)
        return None
    return obj


def _entity_clone_source_register(clone_key, root_obj):
    try:
        root_obj[_ENTITY_CLONE_KEY_PROP] = clone_key
        name = root_obj.name
    except Exception:
        return
    if len(_ENTITY_CLONE_SOURCE_NAMES) > 4096:
        _ENTITY_CLONE_SOURCE_NAMES.clear()
    _ENTITY_CLONE_SOURCE_NAMES[clone_key] = name


def _entity_asset_clone_key(item):
    if bool(item.get("entity_asset_overlay", False)):
        return ""
    if str(item.get("shared_armature_item_id", "") or "").strip():
        return ""
    data_hash = str(item.get("entity_data_hash", "") or "").strip()
    if not data_hash:
        # Backfill hashes for older cached plans.
        entity_data = item.get("entity_data")
        if isinstance(entity_data, dict):
            data_hash = _hash_plan_entity_data(entity_data)
            if data_hash:
                item["entity_data_hash"] = data_hash
    if not data_hash:
        return ""
    try:
        overrides_key = json.dumps(item.get("instance_overrides") or {}, sort_keys=True)
    except Exception:
        return ""
    return "|".join((
        data_hash,
        str(item.get("selected_appearance", "") or ""),
        str(item.get("entity_namespace", "") or ""),
        overrides_key,
    ))


def _clone_entity_asset_plan_item(
    source_root,
    item,
    target_collection,
    parent_obj,
    owner_tag,
    item_id,
    mode_signature,
    kwargs,
):
    if source_root is None or parent_obj is None or target_collection is None:
        return None
    try:
        source_objects = [source_root] + list(getattr(source_root, "children_recursive", None) or [])
    except ReferenceError:
        return None

    clone_pairs = []
    clone_by_id = {}
    try:
        for source_obj in source_objects:
            clone_obj = source_obj.copy()
            target_collection.objects.link(clone_obj)
            clone_pairs.append((source_obj, clone_obj))
            clone_by_id[_object_identity(source_obj)] = clone_obj

        for source_obj, clone_obj in clone_pairs:
            if source_obj is source_root:
                clone_obj.parent = parent_obj
            else:
                clone_obj.parent = clone_by_id.get(_object_identity(getattr(source_obj, "parent", None)))
            _set_object_local_matrix_direct(
                clone_obj,
                source_obj.matrix_basis.copy(),
                source_obj.matrix_parent_inverse.copy(),
            )

        for _source_obj, clone_obj in clone_pairs:
            for modifier in getattr(clone_obj, "modifiers", []):
                for attr_name in ("object", "mirror_object", "offset_object"):
                    _remap_object_reference(modifier, attr_name, clone_by_id)
            for constraint in getattr(clone_obj, "constraints", []):
                for attr_name in ("target", "space_object"):
                    _remap_object_reference(constraint, attr_name, clone_by_id)
    except Exception:
        log.warning(
            "Entity asset clone failed for %s, falling back to full import.",
            item.get("name", "") or item_id,
            exc_info=True,
        )
        for _source_obj, clone_obj in clone_pairs:
            try:
                bpy.data.objects.remove(clone_obj, do_unlink=True)
            except Exception:
                pass
        return None

    new_root = clone_by_id.get(_object_identity(source_root))
    clone_objects = [clone_obj for _source_obj, clone_obj in clone_pairs]
    _tag_object_tree_for_layer_and_plan(
        new_root,
        owner_tag,
        item_id,
        mode_signature,
        objects=clone_objects,
    )
    execution_results = kwargs.get("_entity_import_execution_results")
    if isinstance(execution_results, dict) and new_root is not None:
        execution_results[item_id] = {
            "root_object": new_root,
            "main_armature": _find_entity_asset_armature(new_root),
            "created_objects": clone_objects,
        }
    return new_root


def _import_cached_plan_full_items(plan, target_collection, kwargs, context=None, loaded_collection=None, errors=None, level_file="", preflight_item_errors=None):
    total_started = time.perf_counter()
    if target_collection is None:
        target_collection = _get_active_collection(context)
    if target_collection is None:
        return 0

    from ..importers import import_entity

    items = [item for item in plan.get("items", []) or [] if isinstance(item, dict)]
    if preflight_item_errors is None:
        _, preflight_item_errors = _entity_import_plan_preflight(plan, deep=False)
    skip_ids = _entity_plan_skip_ids(items, preflight_item_errors)
    if skip_ids:
        items = [
            item for item in items
            if str(item.get("id", "") or "").strip() not in skip_ids
        ]
    by_id = {
        str(item.get("id", "") or "").strip(): item
        for item in items
        if str(item.get("id", "") or "").strip()
    }
    mode_signature = str(kwargs.get("_layer_import_mode_signature", "") or "").strip()
    if not mode_signature:
        mode_signature = _layer_load_mode_signature(False)
    loaded_by_id = (
        {}
        if bool(kwargs.get("_entity_import_disable_loaded_reuse", False))
        else _cached_plan_loaded_item_objects(loaded_collection or target_collection, mode_signature)
    )
    needed_ids = set()
    skipped_loaded = 0
    select_started = time.perf_counter()

    for item in items:
        item_id = str(item.get("id", "") or "").strip()
        kind = str(item.get("kind", "") or "").strip().lower()
        repo_path_value = str(item.get("repo_path", "") or "").strip()
        if not item_id or item_id in loaded_by_id:
            if item_id:
                skipped_loaded += 1
            continue
        if kind in _CACHED_FULL_MESH_ITEM_KINDS:
            if not repo_path_value or not _cached_plan_mesh_enabled(kind, kwargs, item):
                continue
        elif kind in _CACHED_SECTOR_INSTANCER_KINDS:
            if not repo_path_value or not _cached_plan_mesh_enabled(kind, kwargs, item):
                continue
        elif kind in _CACHED_REDCLOTH_ITEM_KINDS:
            if (
                not repo_path_value
                or not _cloth_resource_enabled_for_import(repo_path_value, kwargs, context)
            ):
                continue
        elif kind in _CACHED_FULL_LIGHT_ITEM_KINDS:
            if not _cached_plan_light_enabled(kind, kwargs):
                continue
        elif kind in _CACHED_FULL_EMPTY_ITEM_KINDS:
            if not bool(kwargs.get("do_import_Entity", True)):
                continue
        elif kind in _CACHED_ENTITY_ASSET_ITEM_KINDS:
            has_embedded_entity_data = isinstance(item.get("entity_data"), dict)
            if (
                (not repo_path_value and not has_embedded_entity_data)
                or not bool(kwargs.get("do_import_Entity", True))
            ):
                continue
        else:
            continue

        current = item
        while current is not None:
            current_id = str(current.get("id", "") or "").strip()
            if not current_id or current_id in needed_ids:
                break
            needed_ids.add(current_id)
            parent_id = str(current.get("parent_id", "") or "").strip()
            current = by_id.get(parent_id)
        shared_asset_id = str(item.get("shared_armature_item_id", "") or "").strip()
        current = by_id.get(shared_asset_id)
        while current is not None:
            current_id = str(current.get("id", "") or "").strip()
            if not current_id or current_id in needed_ids:
                break
            needed_ids.add(current_id)
            current = by_id.get(str(current.get("parent_id", "") or "").strip())
    select_seconds = time.perf_counter() - select_started

    created = dict(loaded_by_id)
    owner_tag = kwargs.get("_layer_import_owner")
    parent_seconds = 0.0
    mesh_seconds = 0.0
    cloth_seconds = 0.0
    light_seconds = 0.0
    entity_seconds = 0.0
    parent_count = 0
    mesh_count = 0
    cloth_count = 0
    light_count = 0
    entity_count = 0
    entity_clone_count = 0
    entity_clone_sources = {}
    visibility_parents = {}

    def queue_parent_visibility(parent_obj):
        while parent_obj is not None:
            visibility_parents[_object_identity(parent_obj)] = parent_obj
            parent_obj = getattr(parent_obj, "parent", None)

    def ensure_parent_empty(item_id):
        nonlocal parent_seconds, parent_count
        item_id = str(item_id or "").strip()
        if not item_id or item_id not in needed_ids:
            return None
        existing = created.get(item_id)
        if existing is not None:
            return existing
        item = by_id.get(item_id)
        if item is None:
            return None
        kind = str(item.get("kind", "") or "").strip().lower()
        if kind in _CACHED_FULL_ITEM_KINDS:
            return None
        parent_started = time.perf_counter()
        parent_obj = ensure_parent_empty(str(item.get("parent_id", "") or "").strip())
        obj = _create_linked_empty(item.get("name", "Entity"), target_collection)
        if obj is None:
            return None
        if parent_obj is not None:
            obj.parent = parent_obj
        _apply_plan_item_transform_for_parent(obj, item, parent_obj)
        _tag_single_object_for_layer(obj, owner_tag)
        _tag_object_tree_for_plan_item(obj, item_id, mode_signature)
        try:
            if kind == "entity":
                _set_redkit_entity_metadata(
                    obj,
                    str(item.get("entity_class", "") or "CEntity"),
                    entity_name=str(item.get("name", "") or ""),
                    template_path=str(item.get("repo_path", "") or ""),
                    action_name=str(item.get("action_name", "") or ""),
                    instance_id=str(item.get("instance_id", "") or ""),
                    entity_guid=str(item.get("entity_guid", "") or ""),
                    entity_id=str(item.get("entity_id", "") or ""),
                )
                if "engine_visible" in item:
                    obj["witcher_entity_instance_visible"] = bool(item["engine_visible"])
                    _tag_object_tree_engine_visibility(obj, item["engine_visible"], kwargs)
            obj["witcher_cached_plan_proxy"] = True
            obj["witcher_cached_plan_kind"] = kind
        except Exception:
            pass
        created[item_id] = obj
        parent_count += 1
        parent_seconds += time.perf_counter() - parent_started
        return obj

    if errors is None:
        errors = []
    keep_lod_meshes = bool(kwargs.get("keep_lod_meshes", False))
    keep_proxy_meshes = bool(kwargs.get("keep_proxy_meshes", True))
    imported_count = 0
    shared_snapshot = None
    for item in items:
        _raise_if_layer_import_cancelled(kwargs)
        item_id = str(item.get("id", "") or "").strip()
        if not item_id or item_id not in needed_ids or item_id in loaded_by_id:
            continue
        kind = str(item.get("kind", "") or "").strip().lower()
        parent_count_before = parent_count
        parent_obj = ensure_parent_empty(str(item.get("parent_id", "") or "").strip())
        if parent_count != parent_count_before or kind not in _CACHED_ENTITY_ASSET_ITEM_KINDS:
            shared_snapshot = None

        if kind in _CACHED_ENTITY_ASSET_ITEM_KINDS:
            shared_armature = None
            shared_asset_id = str(item.get("shared_armature_item_id", "") or "").strip()
            if shared_asset_id:
                execution_results = kwargs.get("_entity_import_execution_results")
                if isinstance(execution_results, dict):
                    shared_armature = (
                        execution_results.get(shared_asset_id, {}) or {}
                    ).get("main_armature")
                if shared_armature is None:
                    shared_armature = _find_entity_asset_armature(
                        created.get(shared_asset_id) or loaded_by_id.get(shared_asset_id)
                    )
            entity_started = time.perf_counter()
            clone_key = _entity_asset_clone_key(item) if parent_obj is not None else ""
            root_obj = None
            if clone_key:
                clone_source = entity_clone_sources.get(clone_key)
                if clone_source is None:
                    clone_source = _entity_clone_source_lookup(clone_key)
                if clone_source is not None:
                    shared_snapshot = None
                    root_obj = _clone_entity_asset_plan_item(
                        clone_source,
                        item,
                        target_collection,
                        parent_obj,
                        owner_tag,
                        item_id,
                        mode_signature,
                        kwargs,
                    )
                    if root_obj is not None:
                        entity_clone_count += 1
                        entity_clone_sources.setdefault(clone_key, clone_source)
            if root_obj is None:
                snapshot_out = {}
                root_obj = _materialize_entity_asset_plan_item(
                    item,
                    target_collection,
                    parent_obj,
                    owner_tag,
                    item_id,
                    mode_signature,
                    kwargs,
                    errors,
                    import_entity,
                    context=context,
                    shared_armature=shared_armature,
                    before_snapshot=shared_snapshot,
                    snapshot_out=snapshot_out,
                )
                shared_snapshot = snapshot_out.get("after")
                if root_obj is not None and clone_key:
                    entity_clone_sources.setdefault(clone_key, root_obj)
                    _entity_clone_source_register(clone_key, root_obj)
            entity_seconds += time.perf_counter() - entity_started
            if root_obj is None:
                continue
            entity_count += 1
            loaded_by_id[item_id] = root_obj
            created[item_id] = root_obj
            imported_count += 1
            continue

        if kind in _CACHED_SECTOR_INSTANCER_KINDS:
            mesh_started = time.perf_counter()
            root_obj = _import_cached_plan_sector_instancer_item(
                item,
                target_collection,
                parent_obj,
                owner_tag,
                item_id,
                mode_signature,
                kwargs,
                errors,
                context=context,
            )
            mesh_seconds += time.perf_counter() - mesh_started
            if root_obj is None:
                continue
            if parent_obj is not None:
                _apply_plan_item_transform_as_child(root_obj, item)
            loaded_by_id[item_id] = root_obj
            created[item_id] = root_obj
            queue_parent_visibility(parent_obj)
            imported_count += 1
            mesh_count += 1
            continue

        if kind in _CACHED_FULL_MESH_ITEM_KINDS:
            mesh = _cached_plan_mesh_from_item(item, kind, context=context)
            if mesh is None:
                continue
            try:
                mesh_started = time.perf_counter()
                mesh_kwargs = dict(kwargs)
                mesh_kwargs.pop("keep_lod_meshes", None)
                mesh_kwargs["_layer_import_plan_item_id"] = item_id
                mesh_kwargs["_layer_import_plan_mode"] = mode_signature
                mesh_kwargs["_cached_plan_fast_static_clone"] = True
                if kind == "collision":
                    root_obj = _import_sector_collision_from_cache(
                        mesh, errors, parent_obj, **mesh_kwargs,
                    )
                elif kind == "component_mesh":
                    component_type = _component_type_from_plan_item(item)
                    component_name = str(item.get("component_name", "") or "")
                    if not component_name:
                        item_name = str(item.get("name", "") or "")
                        prefix = component_type + " "
                        if item_name.startswith(prefix):
                            component_name = item_name[len(prefix):].strip()
                    mesh.import_name = Path(str(item.get("repo_path", "") or "").replace("\\", "/")).stem or str(item.get("name", "") or "Mesh")
                    root_obj = _import_component_mesh_from_mesh(
                        mesh,
                        errors,
                        parent_obj,
                        component_type=component_type,
                        component_name=component_name,
                        component_transform=item.get("transform") or None,
                        drawable_flags=item.get("drawable_flags") if "drawable_flags" in item else None,
                        engine_visible=item.get("engine_visible") if "engine_visible" in item else None,
                        action_name=str(item.get("action_name", "") or ""),
                        target_collection=target_collection,
                        keep_lod_meshes=keep_lod_meshes or (keep_proxy_meshes and _cached_plan_item_is_proxy_mesh(item)),
                        kwargs=mesh_kwargs,
                    )
                else:
                    root_obj = import_single_mesh(
                        mesh,
                        errors,
                        parent_obj,
                        keep_lod_meshes=keep_lod_meshes or (keep_proxy_meshes and _cached_plan_item_is_proxy_mesh(item)),
                        **mesh_kwargs,
                    )
                mesh_seconds += time.perf_counter() - mesh_started
            except Exception as exc:
                log.warning("Problem with cached mesh import %s: %s", mesh.meshName, exc)
                errors.append(f"Problem with cached mesh import {mesh.meshName}: {exc}")
                continue
            if root_obj is None:
                continue
            loaded_by_id[item_id] = root_obj
            created[item_id] = root_obj
            queue_parent_visibility(parent_obj)
            imported_count += 1
            mesh_count += 1
            continue

        if kind in _CACHED_FULL_LIGHT_ITEM_KINDS:
            if not _cached_plan_light_enabled(kind, kwargs):
                continue
            light_started = time.perf_counter()
            root_obj = _import_cached_plan_light_item(
                item,
                kind,
                target_collection,
                parent_obj,
                owner_tag,
                item_id,
                mode_signature,
            )
            light_seconds += time.perf_counter() - light_started
            loaded_by_id[item_id] = root_obj
            created[item_id] = root_obj
            imported_count += 1
            light_count += 1
            continue

        if kind in _CACHED_FULL_EMPTY_ITEM_KINDS:
            parent_started = time.perf_counter()
            root_obj = _import_cached_plan_empty_item(
                item,
                target_collection,
                parent_obj,
                owner_tag,
                item_id,
                mode_signature,
                kwargs,
            )
            parent_seconds += time.perf_counter() - parent_started
            if root_obj is None:
                continue
            loaded_by_id[item_id] = root_obj
            created[item_id] = root_obj
            imported_count += 1
            parent_count += 1
            continue

        if kind in _CACHED_REDCLOTH_ITEM_KINDS:
            resource = str(item.get("repo_path", "") or "").strip()
            if (
                not resource
                or not _cloth_resource_enabled_for_import(resource, kwargs, context)
            ):
                continue
            try:
                cloth_started = time.perf_counter()
                if _is_redapex_resource(resource):
                    root_obj, _cloth_meshes = import_entity.import_or_reuse_redapex(
                        resource,
                        repo_file(resource),
                        context=context or bpy.context,
                        loadmods=bool(kwargs.get("loadmods", False)),
                        target_collection=target_collection,
                        **_redapex_import_options(kwargs),
                    )
                    cloth_arma = root_obj
                    cloth_grp = None
                else:
                    cloth_arma, cloth_grp, _cloth_meshes = import_entity.import_or_reuse_redcloth(
                        parent_obj,
                        resource,
                        repo_file(resource),
                        import_name="CClothComponent",
                        entity_name=str(item.get("name", "") or Path(resource.replace("/", "\\")).stem),
                        target_collection=target_collection,
                        hide_collision_proxies=bool(kwargs.get("hide_proxy_meshes", False)),
                    )
                cloth_seconds += time.perf_counter() - cloth_started
            except Exception as exc:
                resource_label = "redapex" if _is_redapex_resource(resource) else "redcloth"
                log.warning("Problem with cached %s import %s: %s", resource_label, resource, exc)
                errors.append(f"Problem with cached {resource_label} import {resource}: {exc}")
                continue
            failure_reason = _cloth_import_failure_reason(cloth_arma)
            if failure_reason:
                resource_label = "redapex" if _is_redapex_resource(resource) else "redcloth"
                message = f"Problem with cached {resource_label} import {resource}: {failure_reason}"
                log.warning("%s", message)
                errors.append(message)
                continue
            root_obj = cloth_grp if cloth_grp is not None else cloth_arma
            _apply_cached_destruction_metadata(root_obj, item, kwargs)
            _apply_requested_proxy_helper_visibility(root_obj, kwargs)
            if parent_obj is not None:
                root_obj.parent = parent_obj
                _apply_plan_item_transform_as_child(root_obj, item)
            else:
                _apply_plan_item_transform(root_obj, item)
            _tag_object_tree_for_layer_and_plan(
                root_obj,
                owner_tag,
                item_id,
                mode_signature,
            )
            loaded_by_id[item_id] = root_obj
            created[item_id] = root_obj
            imported_count += 1
            cloth_count += 1

    children_map = None
    for item_id, obj in list(created.items()):
        item = by_id.get(str(item_id or ""))
        if not isinstance(item, dict):
            continue
        if str(item.get("kind", "") or "").strip().lower() == "entity":
            if "engine_visible" in item:
                if children_map is None:
                    children_map = _build_object_children_map()
                subtree = [obj]
                stack = [obj]
                while stack:
                    current = stack.pop()
                    for child in children_map.get(_object_identity(current), []):
                        subtree.append(child)
                        stack.append(child)
                _tag_object_tree_engine_visibility(obj, item["engine_visible"], kwargs, objects=subtree)
            else:
                queue_parent_visibility(obj)

    # Aggregate visibility after all children are created.
    if visibility_parents:
        if children_map is None:
            children_map = _build_object_children_map()
        for parent_obj in sorted(
            visibility_parents.values(),
            key=_object_parent_depth,
            reverse=True,
        ):
            _tag_entity_empty_engine_visibility_from_children(parent_obj, kwargs, children_map=children_map)

    total_seconds = time.perf_counter() - total_started
    if total_seconds >= _LAYER_IMPORT_PROFILE_WARN_THRESHOLD:
        _log_layer_import_profile_warning(
            "cached plan full %s total %.3fs (select %.3fs, parents %.3fs/%d, mesh dispatch %.3fs/%d, cloth dispatch %.3fs/%d, light dispatch %.3fs/%d, entity %.3fs/%d (%d cloned), imported %d, loaded skips %d, source items %d, needed ids %d)",
            level_file or "<cached-plan>",
            total_seconds,
            select_seconds,
            parent_seconds,
            parent_count,
            mesh_seconds,
            mesh_count,
            cloth_seconds,
            cloth_count,
            light_seconds,
            light_count,
            entity_seconds,
            entity_count,
            entity_clone_count,
            imported_count,
            skipped_loaded,
            len(items),
            len(needed_ids),
        )
    return imported_count


def _filter_cached_plan_items_by_proximity(items, nearby_filter, nearby_stats, item_kinds=None):
    """Cull cached plan items while retaining parent chains and instancers."""
    if nearby_filter is None:
        return list(items)

    target_kinds = None
    if item_kinds:
        target_kinds = {str(kind or "").strip().lower() for kind in item_kinds if str(kind or "").strip()}

    by_id = {}
    for item in items:
        item_id = str(item.get("id", "") or "")
        if item_id:
            by_id[item_id] = item

    keep = set()
    filtered_count = 0
    for item in items:
        kind = str(item.get("kind", "") or "").strip().lower()
        if kind == "group":
            continue
        if target_kinds is not None and kind not in target_kinds:
            continue
        if kind == "sector_instancer":
            transforms = item.get("sector_transforms") or []
            any_nearby = False
            for tr in transforms:
                t_pos = tr.get("t") if isinstance(tr, dict) else None
                if t_pos and _position_within_nearby_filter(t_pos, nearby_filter):
                    any_nearby = True
                    break
            if any_nearby:
                keep.add(str(item.get("id", "") or ""))
            elif transforms:
                filtered_count += 1
            continue
        position = item.get("world_position")
        if position is None:
            continue
        if _position_within_nearby_filter(position, nearby_filter):
            keep.add(str(item.get("id", "") or ""))
        else:
            filtered_count += 1

    full_keep = set(keep)
    for kept_id in list(keep):
        current = by_id.get(kept_id)
        while current is not None:
            parent_id = str(current.get("parent_id", "") or "")
            if not parent_id or parent_id in full_keep:
                break
            full_keep.add(parent_id)
            current = by_id.get(parent_id)

    nearby_stats["filtered"] = int(nearby_stats.get("filtered", 0) or 0) + filtered_count
    return [item for item in items if str(item.get("id", "") or "") in full_keep]


def _stable_sector_instancer_id(repo_path, kind, visibility_key=""):
    """Build a deterministic id from (kind, repo_path) so reloads can find the same instancer."""
    import hashlib
    key = f"{str(kind or '').lower()}|{str(repo_path or '').lower().replace('/', chr(92))}|{str(visibility_key or '').lower()}"
    return "_si_" + hashlib.md5(key.encode("utf-8")).hexdigest()[:16]


def _maybe_group_cached_items_into_sector_instancers(items, kwargs):
    """Group repeated top-level sector meshes into instancer items."""
    if not bool(kwargs and kwargs.get("instanced_sector", False)):
        return list(items or [])

    items = list(items or [])
    if not items:
        return items

    grouped = {}  # (kind, repo_path, visibility_key) -> list of items
    survivors = []

    for item in items:
        if not isinstance(item, dict):
            survivors.append(item)
            continue
        kind = str(item.get("kind", "") or "").strip().lower()
        parent_id = str(item.get("parent_id", "") or "").strip()
        repo_path = str(item.get("repo_path", "") or "").strip()
        if parent_id or not repo_path:
            survivors.append(item)
            continue
        if item.get("embedded_cmesh_chunk_index") is not None or item.get("mesh_chunk_indices"):
            survivors.append(item)
            continue
        is_proxy = _cached_plan_item_is_proxy_mesh(item) if kind in {"mesh", "component_mesh"} else False
        if (kind == "mesh" and not is_proxy) or kind in {"rigid", "rigid_body", "collision"}:
            visibility_key = _sector_visibility_key_for_item(kind, item)
            grouped.setdefault((kind, repo_path, visibility_key), []).append(item)
        else:
            survivors.append(item)

    if not grouped:
        return items

    new_items = list(survivors)
    for (kind, repo_path, visibility_key), grouped_items in grouped.items():
        if len(grouped_items) < 2:
            new_items.extend(grouped_items)
            continue
        sector_transforms = []
        for it in grouped_items:
            t_src = it.get("translation") or it.get("local_position") or it.get("world_position") or [0.0, 0.0, 0.0]
            try:
                t = [float(t_src[0]), float(t_src[1]), float(t_src[2])]
            except Exception:
                t = [0.0, 0.0, 0.0]
            m = it.get("matrix")
            if m:
                try:
                    m = [list(r) for r in m]
                except Exception:
                    m = None
            sector_transforms.append({"t": t, "m": m})
        synth = {
            "id": _stable_sector_instancer_id(repo_path, kind, visibility_key),
            "kind": "sector_instancer",
            "name": Path(repo_path.replace("\\", "/")).stem or "Instancer",
            "parent_id": "",
            "repo_path": repo_path,
            "sector_transforms": sector_transforms,
            "_source_kind": kind,
        }
        if visibility_key:
            synth["sector_visibility_key"] = visibility_key
            synth["sector_visible"] = visibility_key != "hidden"
            first_flags = grouped_items[0].get("sector_flags") if grouped_items else None
            if first_flags is not None:
                synth["sector_flags"] = first_flags
        new_items.append(synth)

    return new_items


def _ensure_cached_sector_group_hierarchy(items):
    """Add CSectorData groups to flat cached sector items."""
    source_items = [item for item in items or [] if isinstance(item, dict)]
    if not source_items:
        return []
    for item in source_items:
        if (
            str(item.get("kind", "") or "").strip().lower() == "group"
            and str(item.get("name", "") or "").strip() == "CSectorData"
        ):
            return list(source_items)

    sector_kind_to_group = {
        "mesh": "_sector_mesh",
        "sector_instancer": "_sector_mesh",
        "rigid": "_sector_rigid",
        "rigid_body": "_sector_rigid",
        "collision": "_sector_collision",
        "point_light": "_sector_point_light",
        "spot_light": "_sector_spot_light",
    }
    group_names = {
        "_sector_collision": "Collision",
        "_sector_rigid": "Rigid",
        "_sector_mesh": "Mesh",
        "_sector_point_light": "PointLight",
        "_sector_spot_light": "SpotLight",
    }
    needed_groups = set()
    normalized = []
    for item in source_items:
        copied = dict(item)
        kind = str(copied.get("kind", "") or "").strip().lower()
        source_kind = str(copied.get("_source_kind", "") or "").strip().lower()
        parent_id = str(copied.get("parent_id", "") or "").strip()
        group_id = sector_kind_to_group.get(source_kind if kind == "sector_instancer" else kind)
        if group_id and not parent_id:
            copied["parent_id"] = group_id
            needed_groups.add(group_id)
        normalized.append(copied)

    if not needed_groups:
        return normalized

    groups = [
        {
            "id": "_sector_root",
            "kind": "group",
            "name": "CSectorData",
            "parent_id": "",
            "repo_path": "",
            "transform": None,
            "matrix": None,
            "translation": None,
            "world_position": None,
        }
    ]
    for group_id in ("_sector_collision", "_sector_rigid", "_sector_mesh", "_sector_point_light", "_sector_spot_light"):
        if group_id not in needed_groups:
            continue
        groups.append(
            {
                "id": group_id,
                "kind": "group",
                "name": group_names[group_id],
                "parent_id": "_sector_root",
                "repo_path": "",
                "transform": None,
                "matrix": None,
                "translation": None,
                "world_position": None,
            }
        )
    return groups + normalized


def _resolve_component_import_plan(
    plan,
    component,
    parent_id,
    parent_position=None,
    *,
    mesh_fbx_uncook_path=None,
    mesh_uncook_path=None,
):
    component_name = getattr(component, "name", getattr(component, "Type", "Component"))
    world_position = _chunk_world_position(component, parent_position)
    transform_prop = None
    try:
        transform_prop = component.GetVariableByName('transform')
    except Exception:
        transform_prop = None
    transform = getattr(transform_prop, "EngineTransform", None) if transform_prop else None

    if component_name in {
        "CMeshComponent",
        "CStaticMeshComponent",
        "CDestructionComponent",
    }:
        component_label = _component_prop_string(component, "name")
        action_name = _component_action_name(component)
        try:
            mesh = _new_mesh_path(
                fbx_uncook_path=mesh_fbx_uncook_path,
                uncook_path=mesh_uncook_path,
            ).static_from_chunk(component)
        except MeshReferenceMissing:
            drawable_flags = _component_drawable_flags(component)
            engine_visible = _drawable_flags_visible_from_value(drawable_flags, default=True)
            return _add_level_import_plan_item(
                plan,
                "component_empty",
                _component_label_from_parts(component_name, component_label),
                parent_id=parent_id,
                transform=transform,
                world_position=world_position,
                drawable_flags=drawable_flags,
                engine_visible=engine_visible,
                component_type=component_name,
                component_name=component_label,
                action_name=action_name,
            )
        except Exception as exc:
            raise ValueError(f"Problem resolving mesh component {component_name}: {exc}") from exc
        mesh_path = _mesh_repo_path(mesh)
        if not mesh_path:
            raise ValueError(f"{component_name} resolved to an empty mesh path")
        drawable_flags = _component_drawable_flags(component)
        engine_visible = _drawable_flags_visible_from_value(drawable_flags, default=True)
        return _add_level_import_plan_item(
            plan,
            "component_mesh",
            _component_label_from_parts(
                component_name,
                component_label,
                mesh_label=_mesh_label_from_path(mesh_path),
            ),
            parent_id=parent_id,
            repo_path=mesh_path,
            transform=getattr(mesh, "transform", None),
            matrix=getattr(mesh, "matrix", None),
            translation=getattr(mesh, "translation", None),
            world_position=_mesh_world_position(mesh, parent_position),
            is_proxy_mesh=_path_indicates_proxy_mesh(mesh_path, component_name),
            drawable_flags=drawable_flags,
            engine_visible=engine_visible,
            component_type=component_name,
            component_name=component_label,
            action_name=action_name,
            embedded_cmesh_chunk_index=_embedded_cmesh_chunk_index(mesh),
            cr2w_version=getattr(component, "get_CR2W_version", lambda: 999)(),
            mesh_uncook_path=mesh_uncook_path,
        )

    if component_name == "CPointLightComponent":
        return _add_level_import_plan_item(
            plan,
            "component_point_light",
            _component_prop_string(component, "name") or "PointLightComponent",
            parent_id=parent_id,
            transform=transform,
            world_position=world_position,
            component_type=component_name,
            light_properties=_component_light_properties(component),
        )

    if component_name == "CSpotLightComponent":
        return _add_level_import_plan_item(
            plan,
            "component_spot_light",
            _component_prop_string(component, "name") or "SpotLightComponent",
            parent_id=parent_id,
            transform=transform,
            world_position=world_position,
            component_type=component_name,
            light_properties=_component_light_properties(component),
        )

    return None


def _embedded_entity_asset_has_materializable_content(entity_data):
    if not isinstance(entity_data, dict):
        return False
    static_meshes = entity_data.get("staticMeshes") or {}
    if isinstance(static_meshes, dict) and (static_meshes.get("chunks") or []):
        return True
    if entity_data.get("MovingPhysicalAgentComponent"):
        return True
    return bool(
        entity_data.get("appearances")
        or entity_data.get("coloringEntries")
        or entity_data.get("cookedEffects")
        or entity_data.get("CAnimAnimsetsParam")
        or entity_data.get("CAnimMimicParam")
        or entity_data.get("slots")
    )


def _streaming_entity_asset(entity_object):
    direct_asset = getattr(entity_object, "entityAsset", None)
    direct_error = str(getattr(entity_object, "entityAssetError", "") or "").strip()
    if direct_asset is not None or direct_error:
        return direct_asset, direct_error
    streaming_data = getattr(entity_object, "streamingDataBuffer", None)
    if not streaming_data:
        return None, ""
    return (
        getattr(streaming_data, "entityAsset", None),
        str(getattr(streaming_data, "entityAssetError", "") or "").strip(),
    )


def _augment_entity_asset_for_plan(entity, source_path):
    source_path = str(source_path or "").strip()
    if entity is None or not source_path:
        return
    from .dlc_mounters import (
        append_dlc_entity_template_params,
        append_dlc_external_appearances,
    )

    append_dlc_external_appearances(
        entity,
        source_path,
        load_appearances=True,
    )
    append_dlc_entity_template_params(entity, source_path)


def _add_embedded_entity_plan_components(
    plan,
    components,
    *,
    parent_id,
    world_position=None,
):
    added = 0
    for component in components or []:
        if not isinstance(component, dict):
            plan.setdefault("unsupported", []).append(
                "embedded entity plan component is not an object"
            )
            continue
        kind = str(component.get("kind", "") or "").strip().lower()
        component_type = str(component.get("component_type", "") or "").strip()
        repo_path = str(component.get("repo_path", "") or "").strip()
        if kind not in {"component_mesh", "cloth"} or not repo_path:
            plan.setdefault("unsupported", []).append(
                f"unsupported embedded entity plan component {component_type or kind or '<unknown>'}"
            )
            continue
        drawable_flags = component.get("drawable_flags")
        _add_level_import_plan_item(
            plan,
            kind,
            str(component.get("name", "") or "").strip()
            or Path(repo_path.replace("\\", "/")).stem
            or component_type,
            parent_id=parent_id,
            repo_path=repo_path,
            transform=component.get("transform"),
            matrix=component.get("matrix"),
            translation=component.get("translation"),
            world_position=world_position,
            drawable_flags=drawable_flags,
            engine_visible=_drawable_flags_visible_from_value(drawable_flags, default=True),
            component_type=component_type,
            component_name=str(component.get("component_name", "") or "").strip(),
            action_name=str(component.get("action_name", "") or "").strip(),
        )
        added += 1
    return added


def _hash_plan_entity_data(entity_data):
    try:
        payload = json.dumps(entity_data, sort_keys=True, separators=(",", ":"))
    except Exception:
        return ""
    return hashlib.sha1(payload.encode("utf-8", "ignore")).hexdigest()


def _serialize_entity_asset_for_plan(entity, serialized_assets, source_path):
    serialized_key = id(entity)
    cached = (
        serialized_assets.get(serialized_key)
        if isinstance(serialized_assets, dict)
        else None
    )
    if cached is None:
        entity_data = _json_safe_plan_value(entity)
        if not isinstance(entity_data, dict):
            raise ValueError(f"Entity template did not produce JSON-safe data: {source_path}")
        try:
            payload = json.dumps(entity_data, sort_keys=True, separators=(",", ":"))
        except Exception as exc:
            raise ValueError(
                f"Entity template data is not JSON-safe ({source_path}): {exc}"
            ) from exc
        cached = (entity_data, hashlib.sha1(payload.encode("utf-8", "ignore")).hexdigest())
        if isinstance(serialized_assets, dict):
            serialized_assets[serialized_key] = cached
    entity_data, entity_data_hash = cached
    entity_data = dict(entity_data)
    plan_components = list(entity_data.pop("plan_components", []) or [])
    return entity_data, plan_components, entity_data_hash


def _resolve_gameplay_entity_import_plan(
    plan,
    ENTITY_OBJECT,
    *,
    parent_id="",
    parent_position=None,
    instance_world_position=None,
    flatten_entity_into_parent=False,
    keep_lod_meshes=False,
    mesh_fbx_uncook_path=None,
    mesh_uncook_path=None,
    source_context_path="",
    **kwargs,
):
    inline_rich_entity, inline_rich_error = _streaming_entity_asset(ENTITY_OBJECT)
    if inline_rich_error:
        plan.setdefault("unsupported", []).append(
            f"{source_context_path or getattr(ENTITY_OBJECT, 'name', '<inline entity>')}: "
            f"{inline_rich_error}"
        )
    if inline_rich_entity is None:
        mesh_list, cloth_list = getDataBufferMesh(
            ENTITY_OBJECT,
            mesh_fbx_uncook_path=mesh_fbx_uncook_path,
            mesh_uncook_path=mesh_uncook_path,
        )
    else:
        mesh_list, cloth_list = [], []

    source_mesh_count = len(mesh_list or [])
    source_cloth_count = len(cloth_list or [])
    nearby_filter = _get_nearby_import_filter(kwargs)
    nearby_stats = _get_nearby_import_stats(kwargs)
    entity_world_position = (
        _copy_world_position(instance_world_position)
        if instance_world_position is not None
        else _entity_world_position(ENTITY_OBJECT, parent_position)
    )
    anchor_position = entity_world_position or parent_position
    instance_spec = EntityInstanceSpec.from_layer_entity(
        ENTITY_OBJECT,
        source_path=source_context_path,
        parent_id=parent_id,
        world_position=entity_world_position,
    )
    for unsupported_component in (
        getattr(ENTITY_OBJECT, "unsupportedComponents", None) or []
    ):
        plan.setdefault("unsupported", []).append(
            f"{source_context_path or instance_spec.name}: unsupported entity component "
            f"{unsupported_component}"
        )

    supported_component_names = {
        "CMeshComponent",
        "CStaticMeshComponent",
        "CDestructionComponent",
        "CPointLightComponent",
        "CSpotLightComponent",
    }

    filtered_mesh_list = []
    for mesh in mesh_list:
        mesh_path = _mesh_repo_path(mesh)
        if not mesh_path:
            raise ValueError(
                "Gameplay entity mesh resolved to an empty path: "
                f"{getattr(ENTITY_OBJECT, 'name', '') or getattr(ENTITY_OBJECT, 'type', '')}"
            )
        if not _position_within_nearby_filter(_mesh_world_position(mesh, anchor_position), nearby_filter):
            _note_nearby_filter_skip(nearby_stats)
            continue
        filtered_mesh_list.append(mesh)
    mesh_list = filtered_mesh_list

    filtered_cloth_list = []
    for chunk in cloth_list:
        if not _position_within_nearby_filter(_chunk_world_position(chunk, anchor_position), nearby_filter):
            _note_nearby_filter_skip(nearby_stats)
            continue
        filtered_cloth_list.append(chunk)
    cloth_list = filtered_cloth_list

    eligible_components = []
    supported_component_source_count = 0
    component_sources = list(getattr(ENTITY_OBJECT, "Components", None) or [])
    if inline_rich_entity is None:
        component_sources.extend(
            chunk
            for chunk in (
                getattr(
                    getattr(getattr(ENTITY_OBJECT, "streamingDataBuffer", None), "CHUNKS", None),
                    "CHUNKS",
                    None,
                )
                or []
            )
            if str(getattr(chunk, "name", "") or getattr(chunk, "Type", "") or "")
            in {"CPointLightComponent", "CSpotLightComponent"}
        )
    for component in component_sources:
        component_name = getattr(component, "name", None) or getattr(component, "Type", "")
        if component_name in {"CClothComponent", "CDestructionSystemComponent"}:
            supported_component_source_count += 1
            if not _position_within_nearby_filter(_chunk_world_position(component, anchor_position), nearby_filter):
                _note_nearby_filter_skip(nearby_stats)
                continue
            cloth_list.append(component)
            continue
        if component_name not in supported_component_names:
            plan.setdefault("unsupported", []).append(
                f"{source_context_path or instance_spec.name}: unsupported direct entity component "
                f"{component_name or '<unknown>'}"
            )
            continue
        supported_component_source_count += 1
        if not _position_within_nearby_filter(_chunk_world_position(component, anchor_position), nearby_filter):
            _note_nearby_filter_skip(nearby_stats)
            continue
        eligible_components.append(component)

    template = getattr(ENTITY_OBJECT, "template", None)
    instance_rich_entity = inline_rich_entity
    rich_template_entity = instance_rich_entity
    rich_asset_source_path = source_context_path
    if getattr(ENTITY_OBJECT, "isCreatedFromTemplate", False):
        rich_template_entity = getattr(template, "entityAsset", None)
        rich_asset_source_path = instance_spec.template_path
        if rich_template_entity is None and (
            template is not None
            and (
                template.__class__.__name__ == "Entity"
                or hasattr(template, "appearances")
            )
        ):
            rich_template_entity = template
    instance_rich_source_path = (
        f"{source_context_path or instance_spec.source_path or '<layer>'}"
        f"#entity={instance_spec.identity or instance_spec.name or instance_spec.entity_class}"
    )
    secondary_instance_rich_entity = (
        instance_rich_entity
        if (
            getattr(ENTITY_OBJECT, "isCreatedFromTemplate", False)
            and instance_rich_entity is not None
            and instance_rich_entity is not rich_template_entity
        )
        else None
    )
    materialized_entity_class = str(
        getattr(rich_template_entity, "type", "")
        or instance_spec.entity_class
        or "CEntity"
    )
    rich_template_data = None
    rich_template_data_hash = ""
    embedded_plan_components = []
    instance_rich_data = None
    instance_plan_components = []
    if rich_template_entity is not None:
        if not _position_within_nearby_filter(entity_world_position, nearby_filter):
            _note_nearby_filter_skip(nearby_stats)
            return None
        if getattr(ENTITY_OBJECT, "isCreatedFromTemplate", False):
            prepared_assets = kwargs.get("_entity_plan_prepared_assets")
            prepared_key = id(rich_template_entity)
            if not isinstance(prepared_assets, set) or prepared_key not in prepared_assets:
                _augment_entity_asset_for_plan(
                    rich_template_entity,
                    getattr(template, "layerNode", "") or rich_asset_source_path,
                )
                if isinstance(prepared_assets, set):
                    prepared_assets.add(prepared_key)
        rich_template_data, embedded_plan_components, rich_template_data_hash = _serialize_entity_asset_for_plan(
            rich_template_entity,
            kwargs.get("_entity_plan_serialized_assets"),
            rich_asset_source_path or instance_spec.template_path,
        )
        for unsupported_component in rich_template_data.get("unsupported_components", []) or []:
            plan.setdefault("unsupported", []).append(
                f"{rich_asset_source_path or instance_spec.name}: unsupported entity component "
                f"{unsupported_component}"
            )

    instance_rich_hash = ""
    if secondary_instance_rich_entity is not None:
        instance_rich_data, instance_plan_components, instance_rich_hash = _serialize_entity_asset_for_plan(
            secondary_instance_rich_entity,
            kwargs.get("_entity_plan_serialized_assets"),
            instance_rich_source_path,
        )
        for unsupported_component in instance_rich_data.get("unsupported_components", []) or []:
            plan.setdefault("unsupported", []).append(
                f"{instance_rich_source_path}: unsupported entity component "
                f"{unsupported_component}"
            )

    rich_template_data, instance_rich_data = _merge_instance_entity_metadata(
        rich_template_data,
        instance_rich_data,
    )
    if instance_rich_data is not None:
        # Combine cached inputs instead of serializing the merged payload.
        rich_template_data_hash = (
            _hash_plan_entity_data([rich_template_data_hash, instance_rich_hash])
            if rich_template_data_hash and instance_rich_hash
            else ""
        )
    override_partitions, override_partition_errors = _partition_entity_instance_overrides(
        instance_spec.instance_overrides,
        rich_template_data,
        instance_rich_data,
    )
    rich_template_overrides, instance_rich_overrides = override_partitions
    for override_error in override_partition_errors:
        plan.setdefault("unsupported", []).append(
            f"{source_context_path or instance_spec.name}: {override_error}"
        )

    rich_template_has_content = _embedded_entity_asset_has_materializable_content(
        rich_template_data
    )
    instance_rich_has_content = _embedded_entity_asset_has_materializable_content(
        instance_rich_data
    )

    template_mesh_list = (
        []
        if rich_template_data is not None
        else _static_mesh_component_paths_from_template_source(
            template,
            getattr(ENTITY_OBJECT, "templatePath", "") if getattr(ENTITY_OBJECT, "isCreatedFromTemplate", False) else "",
            mesh_fbx_uncook_path=mesh_fbx_uncook_path,
            mesh_uncook_path=mesh_uncook_path,
            source_context_path=source_context_path,
        ) if template is not None else []
    )
    source_template_mesh_count = len(template_mesh_list or [])
    filtered_template_mesh_list = []
    for mesh in template_mesh_list:
        if not _position_within_nearby_filter(_mesh_world_position(mesh, anchor_position), nearby_filter):
            _note_nearby_filter_skip(nearby_stats)
            continue
        filtered_template_mesh_list.append(mesh)
    template_mesh_list = filtered_template_mesh_list
    has_template_child_content = bool(
        template is not None
        and (getattr(template, "includes", None) or getattr(template, "Entities", None))
    )
    has_template_content = bool(
        rich_template_has_content
        or embedded_plan_components
        or instance_rich_has_content
        or instance_plan_components
        or (template is not None and (template_mesh_list or has_template_child_content))
    )
    if not mesh_list and not cloth_list and not eligible_components and not has_template_content:
        if flatten_entity_into_parent:
            return None
        if (
            source_mesh_count <= 0
            and source_cloth_count <= 0
            and supported_component_source_count <= 0
            and source_template_mesh_count <= 0
        ):
            return _add_level_import_plan_item(
                plan,
                "entity_empty",
                instance_spec.name or instance_spec.entity_class or "Entity",
                parent_id=parent_id,
                repo_path=instance_spec.template_path,
                transform=instance_spec.transform,
                world_position=entity_world_position,
                action_name=instance_spec.action_name,
                entity_class=materialized_entity_class,
                instance_id=instance_spec.identity,
                entity_guid=instance_spec.guid,
                entity_id=instance_spec.entity_id,
                instance_overrides=instance_spec.instance_overrides,
                source_path=instance_spec.source_path,
                engine_visible=instance_spec.engine_visible,
            )
        return None

    if flatten_entity_into_parent:
        entity_id = parent_id
    else:
        entity_id = _add_level_import_plan_item(
            plan,
            "entity",
            instance_spec.name or instance_spec.entity_class or "Entity",
            parent_id=parent_id,
            repo_path=instance_spec.template_path,
            transform=instance_spec.transform,
            world_position=entity_world_position,
            action_name=instance_spec.action_name,
            entity_class=materialized_entity_class,
            instance_id=instance_spec.identity,
            entity_guid=instance_spec.guid,
            entity_id=instance_spec.entity_id,
            instance_overrides=instance_spec.instance_overrides,
            source_path=instance_spec.source_path,
            engine_visible=instance_spec.engine_visible,
        )
    items_before_children = len(plan["items"])

    rich_template_item_id = ""
    if rich_template_data is not None and rich_template_has_content:
        rich_template_item_id = _add_level_import_plan_item(
            plan,
            "entity_asset",
            str(getattr(rich_template_entity, "name", "") or "").strip()
            or Path((rich_asset_source_path or instance_spec.name).replace("\\", "/")).stem
            or "Entity",
            parent_id=entity_id,
            repo_path=rich_asset_source_path,
            source_path=rich_asset_source_path,
            world_position=entity_world_position,
            entity_class=str(getattr(rich_template_entity, "type", "") or instance_spec.entity_class or "CEntity"),
            instance_id=instance_spec.identity,
            entity_guid=instance_spec.guid,
            entity_id=instance_spec.entity_id,
            instance_overrides=rich_template_overrides,
            selected_appearance=str(kwargs.get("selected_appearance", "") or ""),
            entity_data=rich_template_data,
            entity_data_hash=rich_template_data_hash,
        )

    instance_rich_item_id = ""
    if instance_rich_data is not None and instance_rich_has_content:
        instance_rich_item_id = _add_level_import_plan_item(
            plan,
            "entity_asset",
            str(getattr(secondary_instance_rich_entity, "name", "") or "").strip()
            or f"{instance_spec.name or instance_spec.entity_class} instance graph",
            parent_id=entity_id,
            repo_path=instance_rich_source_path,
            source_path=instance_rich_source_path,
            world_position=entity_world_position,
            entity_class=str(
                getattr(secondary_instance_rich_entity, "type", "")
                or instance_spec.entity_class
                or "CEntity"
            ),
            instance_id=instance_spec.identity,
            entity_guid=instance_spec.guid,
            entity_id=instance_spec.entity_id,
            instance_overrides=instance_rich_overrides,
            entity_asset_overlay=bool(rich_template_item_id),
            shared_armature_item_id=rich_template_item_id,
            entity_data=instance_rich_data,
            entity_data_hash="" if rich_template_item_id else instance_rich_hash,
        )

    if not flatten_entity_into_parent and (rich_template_item_id or instance_rich_item_id):
        entity_item = next(
            (item for item in plan["items"] if item.get("id") == entity_id),
            None,
        )
        if entity_item is not None:
            entity_item.pop("instance_overrides", None)

    _add_embedded_entity_plan_components(
        plan,
        embedded_plan_components,
        parent_id=entity_id,
        world_position=anchor_position,
    )
    _add_embedded_entity_plan_components(
        plan,
        instance_plan_components,
        parent_id=entity_id,
        world_position=anchor_position,
    )

    for mesh in template_mesh_list:
        mesh_path = _mesh_repo_path(mesh)
        if not mesh_path:
            continue
        component_type = str(getattr(mesh, "component_type", "") or "CMeshComponent").strip()
        component_name = str(getattr(mesh, "component_name", "") or "").strip()
        action_name = str(getattr(mesh, "component_action_name", "") or "").strip()
        _add_level_import_plan_item(
            plan,
            "component_mesh",
            _component_label_from_parts(
                component_type,
                component_name,
                mesh_label=_mesh_label_from_path(mesh_path),
            ),
            parent_id=entity_id,
            repo_path=mesh_path,
            transform=getattr(mesh, "transform", None),
            matrix=getattr(mesh, "matrix", None),
            translation=getattr(mesh, "translation", None),
            world_position=anchor_position,
            is_proxy_mesh=bool(getattr(mesh, "is_proxy_mesh", False)) or _path_indicates_proxy_mesh(mesh_path, component_name),
            drawable_flags=getattr(mesh, "drawable_flags", None) if hasattr(mesh, "drawable_flags") else None,
            engine_visible=getattr(mesh, "engine_visible", None) if hasattr(mesh, "engine_visible") else None,
            component_type=component_type,
            component_name=component_name,
            action_name=action_name,
            embedded_cmesh_chunk_index=_embedded_cmesh_chunk_index(mesh),
        )

    for mesh in mesh_list:
        mesh_path = _mesh_repo_path(mesh)
        if not mesh_path:
            raise ValueError(
                "Gameplay entity mesh resolved to an empty path: "
                f"{getattr(ENTITY_OBJECT, 'name', '') or getattr(ENTITY_OBJECT, 'type', '')}"
            )
        is_proxy_mesh = _path_indicates_proxy_mesh(mesh_path, "")
        component_type = str(getattr(mesh, "component_type", "") or "").strip()
        component_name = str(getattr(mesh, "component_name", "") or "").strip()
        action_name = str(getattr(mesh, "component_action_name", "") or "").strip()
        _add_level_import_plan_item(
            plan,
            "component_mesh" if component_type else "mesh",
            _component_label_from_parts(
                component_type,
                component_name,
                mesh_label=_mesh_label_from_path(mesh_path),
            ) if component_type else Path(mesh_path).stem or "Mesh",
            parent_id=entity_id,
            repo_path=mesh_path,
            transform=getattr(mesh, "transform", None),
            matrix=getattr(mesh, "matrix", None),
            translation=getattr(mesh, "translation", None),
            world_position=_mesh_world_position(mesh, anchor_position),
            is_proxy_mesh=is_proxy_mesh,
            drawable_flags=getattr(mesh, "drawable_flags", None) if hasattr(mesh, "drawable_flags") else None,
            engine_visible=getattr(mesh, "engine_visible", None) if hasattr(mesh, "engine_visible") else None,
            component_type=component_type,
            component_name=component_name,
            action_name=action_name,
            embedded_cmesh_chunk_index=_embedded_cmesh_chunk_index(mesh),
        )

    for chunk in cloth_list:
        cloth_name = getattr(ENTITY_OBJECT, "name", "") or "Cloth"
        cloth_resource = _chunk_cloth_resource(chunk)
        try:
            name_var = chunk.GetVariableByName('name')
            cloth_name = str(getattr(getattr(name_var, "String", None), "String", "") or "").strip() or cloth_name
        except Exception:
            pass
        transform_prop = None
        try:
            transform_prop = chunk.GetVariableByName('transform')
        except Exception:
            transform_prop = None
        component_type = str(getattr(chunk, "name", "") or "").strip()
        component_name = _component_prop_string(chunk, "name")
        drawable_flags = _component_drawable_flags(chunk)
        _add_level_import_plan_item(
            plan,
            "cloth",
            Path(cloth_resource).stem or cloth_name or "Cloth",
            parent_id=entity_id,
            repo_path=cloth_resource,
            transform=getattr(transform_prop, "EngineTransform", None) if transform_prop else None,
            world_position=_chunk_world_position(chunk, anchor_position),
            drawable_flags=drawable_flags,
            engine_visible=_drawable_flags_visible_from_value(drawable_flags, default=True),
            component_type=component_type,
            component_name=component_name,
            action_name=_component_action_name(chunk),
        )

    for component in eligible_components:
        _resolve_component_import_plan(
            plan,
            component,
            entity_id,
            anchor_position,
            mesh_fbx_uncook_path=mesh_fbx_uncook_path,
            mesh_uncook_path=mesh_uncook_path,
        )

    if getattr(ENTITY_OBJECT, "isCreatedFromTemplate", False) and rich_template_data is None:
        include_root_id = ""
        include_items_before = len(plan["items"])
        if template and getattr(template, "includes", None):
            include_root_id = _add_level_import_plan_item(
                plan,
                "group",
                "INCLUDES",
                parent_id=entity_id,
                world_position=anchor_position,
            )
            for INCLUDE_OBJECT in template.includes:
                for inc_entity in getattr(INCLUDE_OBJECT, "Entities", []) or []:
                    if is_entity_chunk(inc_entity):
                        _resolve_gameplay_entity_import_plan(
                            plan,
                            inc_entity,
                            parent_id=include_root_id,
                            parent_position=anchor_position,
                            keep_lod_meshes=keep_lod_meshes,
                            mesh_fbx_uncook_path=mesh_fbx_uncook_path,
                            mesh_uncook_path=mesh_uncook_path,
                            source_context_path=(
                                getattr(INCLUDE_OBJECT, "layerNode", "")
                                or getattr(template, "layerNode", "")
                                or source_context_path
                            ),
                            **kwargs,
                        )
            if len(plan["items"]) == include_items_before + 1:
                _remove_level_import_plan_item(plan, include_root_id)
        for entity in getattr(template, "Entities", []) or []:
            _resolve_gameplay_entity_import_plan(
                plan,
                entity,
                parent_id=entity_id,
                parent_position=anchor_position,
                flatten_entity_into_parent=_is_template_preview_entity(entity),
                keep_lod_meshes=keep_lod_meshes,
                mesh_fbx_uncook_path=mesh_fbx_uncook_path,
                mesh_uncook_path=mesh_uncook_path,
                source_context_path=getattr(template, "layerNode", "") or source_context_path,
                **kwargs,
            )

    if len(plan["items"]) == items_before_children:
        if flatten_entity_into_parent:
            return None
        _remove_level_import_plan_item(plan, entity_id)
        return None
    return entity_id


def level_entity_resolution_errors(level_data):
    errors = []
    seen_errors = set()
    visited = set()

    def add(message):
        message = str(message or "").strip()
        if message and message not in seen_errors:
            seen_errors.add(message)
            errors.append(message)

    def visit(level):
        if level is None or id(level) in visited:
            return
        visited.add(id(level))
        source_path = str(getattr(level, "layerNode", "") or "<memory>")
        expected = getattr(level, "expectedEntityCount", None)
        parsed = getattr(level, "parsedEntityCount", None)
        if expected is not None and parsed is not None and int(expected) != int(parsed):
            add(f"{source_path}: parsed {int(parsed)}/{int(expected)} entity chunks")
        for record in getattr(level, "unresolvedDependencies", None) or []:
            if isinstance(record, dict):
                path = str(record.get("path", "") or "<missing path>")
                reason = str(record.get("reason", "") or "dependency could not be loaded")
                add(f"{source_path}: {path}: {reason}")
            else:
                add(f"{source_path}: {record}")
        for include in getattr(level, "includes", None) or []:
            visit(include)
        for entity in getattr(level, "Entities", None) or []:
            visit(getattr(entity, "template", None))

    visit(level_data)
    return errors


def resolve_level_import_plan(levelData, context = None, keep_lod_meshes:bool = False, **kwargs):
    do_import_Mesh = kwargs.get('do_import_Mesh', True)
    do_import_Collision = kwargs.get('do_import_Collision', True)
    do_import_RigidBody = kwargs.get('do_import_RigidBody', True)
    do_import_PointLight = kwargs.get('do_import_PointLight', True)
    do_import_SpotLight = kwargs.get('do_import_SpotLight', True)
    do_import_Entity = kwargs.get('do_import_Entity', True)
    do_import_ProxyMesh = kwargs.get('do_import_ProxyMesh', False)
    proxy_filter_active = _proxy_mesh_filter_active(kwargs)
    do_enable_name_filter = kwargs.get('do_enable_name_filter', False)
    do_name_filter_regex = kwargs.get('do_name_filter_regex', '')

    nearby_filter = _get_nearby_import_filter(kwargs)
    nearby_stats = _get_nearby_import_stats(kwargs)
    nearby_stats["filtered"] = 0
    kwargs.setdefault("_entity_plan_serialized_assets", {})
    kwargs.setdefault("_entity_plan_prepared_assets", set())
    plan = _new_level_import_plan()
    if do_import_Entity:
        resolution_errors = level_entity_resolution_errors(levelData)
        if resolution_errors:
            plan["unsupported"] = resolution_errors
    level_version = getattr(levelData, "version", 999)
    mesh_fbx_uncook_path = kwargs.get("_mesh_fbx_uncook_path") or kwargs.get("mesh_fbx_uncook_path")
    mesh_uncook_path = kwargs.get("_mesh_uncook_path") or kwargs.get("mesh_uncook_path")
    source_context_path = getattr(levelData, "layerNode", "")
    if not mesh_uncook_path:
        mesh_uncook_path = _derive_w2_uncook_root_from_level_path(getattr(levelData, "layerNode", ""), level_version)

    if levelData.Foliage and do_import_Mesh:
        for treeCollection in (levelData.Foliage.Trees.elements if hasattr(levelData.Foliage, 'Trees') else []):
            treeFilePath = treeCollection.TreeType.DepotPath
            for treeTransform in treeCollection.TreeCollection.elements:
                if not _position_within_nearby_filter(
                    _extract_transform_position(treeTransform),
                    nearby_filter,
                ):
                    _note_nearby_filter_skip(nearby_stats)
                    continue
                _add_level_import_plan_item(
                    plan,
                    "foliage",
                    Path(treeFilePath).stem or "Foliage",
                    repo_path=treeFilePath,
                    transform=treeTransform,
                    world_position=_extract_transform_position(treeTransform),
                )
        for treeCollection in (levelData.Foliage.Grasses.elements if hasattr(levelData.Foliage, 'Grasses') else []):
            treeFilePath = treeCollection.TreeType.DepotPath
            for treeTransform in treeCollection.TreeCollection.elements:
                if not _position_within_nearby_filter(
                    _extract_transform_position(treeTransform),
                    nearby_filter,
                ):
                    _note_nearby_filter_skip(nearby_stats)
                    continue
                _add_level_import_plan_item(
                    plan,
                    "grass",
                    Path(treeFilePath).stem or "Grass",
                    repo_path=treeFilePath,
                    transform=treeTransform,
                    world_position=_extract_transform_position(treeTransform),
                )

    mesh_list = get_CSectorData(
        levelData,
        mesh_fbx_uncook_path=mesh_fbx_uncook_path,
        mesh_uncook_path=mesh_uncook_path,
    )
    if mesh_list:
        mesh_candidates = []
        for mesh in mesh_list:
            mesh_path = _mesh_repo_path(mesh)
            if not mesh_path:
                raise ValueError("CSectorData item resolved to an empty mesh path")
            if not _position_within_nearby_filter(_mesh_world_position(mesh), nearby_filter):
                _note_nearby_filter_skip(nearby_stats)
                continue
            if not (re.search(do_name_filter_regex, mesh.fileName()) if do_enable_name_filter else True):
                continue
            mesh_candidates.append(mesh)

        if mesh_candidates:
            sector_root_id = _add_level_import_plan_item(plan, "group", "CSectorData")
            collision_root_id = _add_level_import_plan_item(plan, "group", "Collision", parent_id=sector_root_id)
            rigid_root_id = _add_level_import_plan_item(plan, "group", "Rigid", parent_id=sector_root_id)
            mesh_root_id = _add_level_import_plan_item(plan, "group", "Mesh", parent_id=sector_root_id)
            point_light_root_id = _add_level_import_plan_item(plan, "group", "PointLight", parent_id=sector_root_id)
            spot_light_root_id = _add_level_import_plan_item(plan, "group", "SpotLight", parent_id=sector_root_id)

            instanced_sector = bool(kwargs.get("instanced_sector", False))
            # Per-repo_path/visibility transform accumulator for GN instancing.
            _sector_instancer_groups: dict = {}  # (kind, repo_path, visibility_key) -> [{"t": [...], "m": [...]}, ...]

            for mesh in mesh_candidates:
                mesh_path = _mesh_repo_path(mesh)
                if not mesh_path:
                    raise ValueError("CSectorData item resolved to an empty mesh path")
                is_proxy_mesh = bool(getattr(mesh, "is_proxy_mesh", False)) or _path_indicates_proxy_mesh(mesh_path, "")
                if mesh.BlockDataObjectType == Enums.BlockDataObjectType.Mesh and (
                    (is_proxy_mesh and proxy_filter_active and do_import_ProxyMesh)
                    or ((not is_proxy_mesh or not proxy_filter_active) and do_import_Mesh)
                ):
                    if instanced_sector and not is_proxy_mesh and _embedded_cmesh_chunk_index(mesh) is None:
                        # Accumulate for GN instancer instead of individual object
                        rp = mesh_path
                        if rp:
                            tv = _extract_vector_position(getattr(mesh, "translation", None))
                            t = list(tv) if tv else [0.0, 0.0, 0.0]
                            m = _copy_matrix_array(getattr(mesh, "matrix", None))
                            m = [list(r) for r in m] if m else None
                            vis_key = _sector_visibility_key_from_flags(getattr(mesh, "sector_flags", None), default=True)
                            _sector_instancer_groups.setdefault(("mesh", rp, vis_key), []).append({"t": t, "m": m})
                    else:
                        _add_level_import_plan_item(
                            plan,
                            "mesh",
                            Path(mesh_path).stem or "Mesh",
                            parent_id=mesh_root_id,
                            repo_path=mesh_path,
                            transform=getattr(mesh, "transform", None),
                            matrix=getattr(mesh, "matrix", None),
                            translation=getattr(mesh, "translation", None),
                            world_position=_mesh_world_position(mesh),
                            is_proxy_mesh=is_proxy_mesh,
                            proxy_role=getattr(mesh, "proxy_role", ""),
                            sector_flags=getattr(mesh, "sector_flags", None),
                            embedded_cmesh_chunk_index=_embedded_cmesh_chunk_index(mesh),
                            cr2w_version=level_version,
                            mesh_uncook_path=mesh_uncook_path,
                        )
                elif mesh.BlockDataObjectType == Enums.BlockDataObjectType.Collision and do_import_Collision:
                    if instanced_sector and _embedded_cmesh_chunk_index(mesh) is None:
                        rp = mesh_path
                        if rp:
                            tv = _extract_vector_position(getattr(mesh, "translation", None))
                            t = list(tv) if tv else [0.0, 0.0, 0.0]
                            m = _copy_matrix_array(getattr(mesh, "matrix", None))
                            m = [list(r) for r in m] if m else None
                            _sector_instancer_groups.setdefault(("collision", rp, ""), []).append({"t": t, "m": m})
                    else:
                        _add_level_import_plan_item(
                            plan,
                            "collision",
                            Path(mesh_path).stem or "Collision",
                            parent_id=collision_root_id,
                            repo_path=mesh_path,
                            transform=getattr(mesh, "transform", None),
                            matrix=getattr(mesh, "matrix", None),
                            translation=getattr(mesh, "translation", None),
                            world_position=_mesh_world_position(mesh),
                            embedded_cmesh_chunk_index=_embedded_cmesh_chunk_index(mesh),
                            cr2w_version=level_version,
                            mesh_uncook_path=mesh_uncook_path,
                        )
                elif mesh.BlockDataObjectType == Enums.BlockDataObjectType.RigidBody and do_import_RigidBody:
                    if instanced_sector and _embedded_cmesh_chunk_index(mesh) is None:
                        rp = mesh_path
                        if rp:
                            tv = _extract_vector_position(getattr(mesh, "translation", None))
                            t = list(tv) if tv else [0.0, 0.0, 0.0]
                            m = _copy_matrix_array(getattr(mesh, "matrix", None))
                            m = [list(r) for r in m] if m else None
                            vis_key = _sector_visibility_key_from_flags(getattr(mesh, "sector_flags", None), default=True)
                            _sector_instancer_groups.setdefault(("rigid", rp, vis_key), []).append({"t": t, "m": m})
                    else:
                        _add_level_import_plan_item(
                            plan,
                            "rigid_body",
                            Path(mesh_path).stem or "RigidBody",
                            parent_id=rigid_root_id,
                            repo_path=mesh_path,
                            transform=getattr(mesh, "transform", None),
                            matrix=getattr(mesh, "matrix", None),
                            translation=getattr(mesh, "translation", None),
                            world_position=_mesh_world_position(mesh),
                            sector_flags=getattr(mesh, "sector_flags", None),
                            embedded_cmesh_chunk_index=_embedded_cmesh_chunk_index(mesh),
                            cr2w_version=level_version,
                            mesh_uncook_path=mesh_uncook_path,
                        )
                elif mesh.BlockDataObjectType == Enums.BlockDataObjectType.PointLight and do_import_PointLight:
                    _add_level_import_plan_item(
                        plan,
                        "point_light",
                        "PointLight",
                        parent_id=point_light_root_id,
                        transform=getattr(mesh, "transform", None),
                        matrix=getattr(mesh, "matrix", None),
                        translation=getattr(mesh, "translation", None),
                        world_position=_mesh_world_position(mesh),
                    )
                elif mesh.BlockDataObjectType == Enums.BlockDataObjectType.SpotLight and do_import_SpotLight:
                    _add_level_import_plan_item(
                        plan,
                        "spot_light",
                        "SpotLight",
                        parent_id=spot_light_root_id,
                        transform=getattr(mesh, "transform", None),
                        matrix=getattr(mesh, "matrix", None),
                        translation=getattr(mesh, "translation", None),
                        world_position=_mesh_world_position(mesh),
                    )

            # Emit one sector_instancer item per unique repo_path
            instancer_parent_by_kind = {
                "mesh": mesh_root_id,
                "rigid": rigid_root_id,
                "rigid_body": rigid_root_id,
                "collision": collision_root_id,
            }
            for (si_kind, rp, vis_key), transforms in _sector_instancer_groups.items():
                _add_level_import_plan_item(
                    plan,
                    "sector_instancer",
                    Path(rp.replace("\\", "/")).stem or "Instancer",
                    parent_id=instancer_parent_by_kind.get(si_kind, mesh_root_id),
                    repo_path=rp,
                    sector_transforms=transforms,
                    sector_visibility_key=vis_key,
                    sector_visible=(vis_key != "hidden"),
                    source_kind=si_kind,
                    cr2w_version=level_version,
                    mesh_uncook_path=mesh_uncook_path,
                )

    if do_import_Entity:
        for INCLUDE_OBJECT in levelData.includes:
            for ENTITY_OBJECT in INCLUDE_OBJECT.Entities:
                if is_entity_chunk(ENTITY_OBJECT):
                    _resolve_gameplay_entity_import_plan(
                        plan,
                        ENTITY_OBJECT,
                        keep_lod_meshes=keep_lod_meshes,
                        mesh_fbx_uncook_path=mesh_fbx_uncook_path,
                        mesh_uncook_path=mesh_uncook_path,
                        source_context_path=getattr(INCLUDE_OBJECT, "layerNode", "") or source_context_path,
                        **kwargs,
                    )

        for ENTITY_OBJECT in levelData.Entities:
            if re.search(do_name_filter_regex, ENTITY_OBJECT.name) if do_enable_name_filter else True:
                if is_entity_chunk(ENTITY_OBJECT):
                    _resolve_gameplay_entity_import_plan(
                        plan,
                        ENTITY_OBJECT,
                        keep_lod_meshes=keep_lod_meshes,
                        mesh_fbx_uncook_path=mesh_fbx_uncook_path,
                        mesh_uncook_path=mesh_uncook_path,
                        source_context_path=source_context_path,
                        **kwargs,
                    )

    plan["stats"]["filtered"] = int(nearby_stats.get("filtered", 0) or 0)
    return plan


def build_entity_import_plan(
    asset=None,
    *,
    instance=None,
    selected_appearance="",
    source_path="",
    options=None,
):
    options = dict(options or {})
    if selected_appearance:
        options.setdefault("selected_appearance", selected_appearance)
    source_path = str(source_path or "").strip()
    source = instance if instance is not None else asset
    if source is None:
        raise ValueError("Entity import requires an asset or instance")
    is_level_source = bool(
        hasattr(source, "Foliage")
        and hasattr(source, "Entities")
        and hasattr(source, "includes")
    )
    is_rich_entity_asset = bool(
        instance is None
        and not is_level_source
        and (
            source.__class__.__name__ == "Entity"
            or hasattr(source, "appearances")
        )
    )

    if is_rich_entity_asset:
        plan = _new_level_import_plan("direct")
        plan["source_path"] = source_path
        item_id = _add_level_import_plan_item(
            plan,
            "entity_asset",
            Path(source_path.replace("\\", "/")).stem
            or str(getattr(source, "name", "") or "Entity"),
            repo_path=source_path,
            selected_appearance=selected_appearance,
            entity_class=str(getattr(source, "type", "") or options.get("entity_class", "") or "CEntity"),
        )
        item = next(item for item in plan["items"] if item.get("id") == item_id)
        item["source_path"] = source_path
        entity_data = _json_safe_plan_value(source)
        plan_components = []
        if isinstance(entity_data, dict):
            entity_data = dict(entity_data)
            plan_components = list(entity_data.pop("plan_components", []) or [])
        item["entity_data"] = entity_data
        if plan_components:
            entity_root_id = _add_level_import_plan_item(
                plan,
                "entity",
                str(getattr(source, "name", "") or "").strip()
                or Path(source_path.replace("\\", "/")).stem
                or "Entity",
                repo_path=source_path,
                entity_class=str(
                    getattr(source, "type", "")
                    or options.get("entity_class", "")
                    or "CEntity"
                ),
                source_path=source_path,
            )
            item["parent_id"] = entity_root_id
            _add_embedded_entity_plan_components(
                plan,
                plan_components,
                parent_id=entity_root_id,
            )
        entity_namespace = str(options.get("entity_namespace", "") or "").strip()
        if entity_namespace:
            item["entity_namespace"] = entity_namespace
        return plan

    if is_level_source:
        plan = resolve_level_import_plan(
            source,
            options.pop("context", None),
            bool(options.pop("keep_lod_meshes", False)),
            **options,
        )
    else:
        plan = _new_level_import_plan()
        _resolve_gameplay_entity_import_plan(
            plan,
            source,
            parent_id=str(options.pop("parent_id", "") or ""),
            parent_position=options.pop("parent_position", None),
            instance_world_position=options.pop("instance_world_position", None),
            flatten_entity_into_parent=bool(options.pop("flatten_entity_into_parent", False)),
            keep_lod_meshes=bool(options.pop("keep_lod_meshes", False)),
            mesh_fbx_uncook_path=options.pop("mesh_fbx_uncook_path", None),
            mesh_uncook_path=options.pop("mesh_uncook_path", None),
            source_context_path=source_path,
            **options,
        )
    if source_path:
        plan["source_path"] = source_path
    return plan


def materialize_entity_import_plan(
    plan,
    *,
    context=None,
    target_collection=None,
    options=None,
    layer_context=None,
    preflight=None,
):
    options = dict(options or {})
    execution_results = options.setdefault("_entity_import_execution_results", {})
    if str(plan.get("mode", "") or "").strip().lower() == "direct":
        options["_entity_import_disable_loaded_reuse"] = True
        options.setdefault("_layer_import_mode_signature", "direct")
    layer_context = dict(layer_context or {})
    errors = layer_context.get("errors")
    if errors is None:
        errors = []

    preflight_fatal, preflight_item_errors = (
        preflight if preflight is not None else _entity_import_plan_preflight(plan, deep=False)
    )
    if preflight_fatal:
        errors.extend(preflight_fatal)
        return EntityImportResult(errors=list(errors))
    if preflight_item_errors:
        for messages in preflight_item_errors.values():
            errors.extend(messages)
        log.warning(
            "Skipping %d invalid entity plan item(s): %s",
            len(preflight_item_errors),
            _preview_list([m for msgs in preflight_item_errors.values() for m in msgs], limit=4),
        )

    if context is None:
        context = bpy.context
    if target_collection is None:
        target_collection = _get_active_collection(context)
    if target_collection is not None:
        _activate_collection(context, target_collection)

    before = _snapshot_blender_objects()
    _import_cached_plan_full_items(
        plan,
        target_collection,
        options,
        context=context,
        loaded_collection=layer_context.get("loaded_collection"),
        errors=errors,
        level_file=str(layer_context.get("level_file", "") or plan.get("source_path", "") or ""),
        preflight_item_errors=preflight_item_errors,
    )
    after = _snapshot_blender_objects()
    created_objects = [obj for key, obj in after.items() if key not in before]
    created_ids = set(after).difference(before)
    root_object = next(
        (
            obj for obj in created_objects
            if getattr(obj, "parent", None) is None
            or _object_identity(getattr(obj, "parent", None)) not in created_ids
        ),
        created_objects[0] if created_objects else None,
    )
    direct_result = None
    if str(plan.get("mode", "") or "").strip().lower() == "direct":
        direct_result = next(iter(execution_results.values()), None)
    if isinstance(direct_result, dict):
        direct_root = direct_result.get("root_object")
        while direct_root is not None:
            parent = getattr(direct_root, "parent", None)
            if parent is None or _object_identity(parent) not in created_ids:
                break
            direct_root = parent
        root_object = direct_root or root_object
        main_armature = direct_result.get("main_armature")
    else:
        main_armature = None
    if main_armature is None:
        main_armature = next(
            (obj for obj in created_objects if str(getattr(obj, "type", "") or "") == "ARMATURE"),
            None,
        )
    created_plan_ids = set()
    for obj in created_objects:
        try:
            item_id = str(obj.get(_LAYER_IMPORT_PLAN_ITEM_PROP, "") or "").strip()
        except Exception:
            item_id = ""
        if item_id:
            created_plan_ids.add(item_id)
    materialized_item_ids = [
        str(item.get("id", "") or "")
        for item in plan.get("items", []) or []
        if str(item.get("id", "") or "") in created_plan_ids
    ]
    return EntityImportResult(
        root_object=root_object,
        main_armature=main_armature,
        created_objects=created_objects,
        materialized_item_ids=materialized_item_ids,
        errors=list(errors),
    )


_REPO_DUPLICATE_CACHE = {
    "scene_key": None,
    "object_count": -1,
    "roots": {},
    "subtrees": {},
}


def _invalidate_duplicate_root_index():
    _REPO_DUPLICATE_CACHE["scene_key"] = None
    _REPO_DUPLICATE_CACHE["object_count"] = -1
    _REPO_DUPLICATE_CACHE["roots"] = {}
    _REPO_DUPLICATE_CACHE["subtrees"] = {}


def _scene_identity(scene):
    if scene is None:
        return None
    try:
        return int(scene.as_pointer())
    except Exception:
        return id(scene)


def _object_identity(obj):
    if obj is None:
        return None
    try:
        return int(obj.as_pointer())
    except Exception:
        return id(obj)


def _is_live_blender_object(obj):
    if obj is None:
        return False
    try:
        return bool(obj.name)
    except ReferenceError:
        return False
    except Exception:
        return False


def _get_scene(context=None):
    ctx = context or bpy.context
    return getattr(ctx, "scene", None)


def _get_active_collection(context=None):
    ctx = context or bpy.context
    collection = getattr(ctx, "collection", None)
    if collection is not None:
        return collection
    view_layer = getattr(ctx, "view_layer", None)
    active_layer_collection = getattr(view_layer, "active_layer_collection", None) if view_layer else None
    collection = getattr(active_layer_collection, "collection", None)
    if collection is not None:
        return collection
    scene = _get_scene(ctx)
    return getattr(scene, "collection", None)


def _normalize_repo_path(path_value):
    return str(path_value or "").replace("/", "\\").strip()


def _normalize_level_repo_path(level_path, context=None):
    norm_path = _normalize_repo_path(level_path)
    uncook_root = _normalize_repo_path(get_uncook_path(context)).rstrip("\\")
    if uncook_root:
        prefix = uncook_root + "\\"
        if norm_path.lower().startswith(prefix.lower()):
            return norm_path[len(prefix):]
    return norm_path


def _derive_w2_uncook_root_from_level_path(level_path, version=999):
    try:
        if int(version) > 115:
            return ""
    except Exception:
        return ""
    norm_path = _normalize_repo_path(level_path)
    if not norm_path or not os.path.isabs(norm_path):
        return ""
    marker = "\\levels\\"
    marker_index = norm_path.lower().find(marker)
    if marker_index <= 0:
        return ""
    root = norm_path[:marker_index].rstrip("\\")
    return root if os.path.isdir(win_safe_path(root)) else ""


def _get_layer_import_owner_tag(level_path, context=None):
    return _normalize_level_repo_path(level_path, context) or _normalize_repo_path(level_path)


def _iter_object_tree(root_obj):
    if root_obj is None:
        return
    stack = [root_obj]
    seen = set()
    while stack:
        obj = stack.pop()
        obj_id = _object_identity(obj)
        if obj_id in seen:
            continue
        seen.add(obj_id)
        yield obj
        stack.extend(list(getattr(obj, "children", []) or []))


def _tag_object_tree_for_layer_and_plan(
    root_obj,
    owner_tag=None,
    item_id="",
    mode_signature="",
    objects=None,
):
    owner_tag = str(owner_tag or "").strip()
    item_id = str(item_id or "").strip()
    mode_signature = str(mode_signature or "").strip()
    if root_obj is None or (not owner_tag and not item_id):
        return
    for obj in (objects if objects is not None else _iter_object_tree(root_obj)):
        try:
            if owner_tag:
                obj[_LAYER_IMPORT_OWNER_PROP] = owner_tag
            if item_id:
                obj[_LAYER_IMPORT_PLAN_ITEM_PROP] = item_id
                if mode_signature:
                    obj[_LAYER_IMPORT_PLAN_MODE_PROP] = mode_signature
        except Exception:
            continue


def _tag_object_tree_for_layer(root_obj, owner_tag=None):
    _tag_object_tree_for_layer_and_plan(root_obj, owner_tag)


def _tag_object_tree_as_proxy_mesh(root_obj, objects=None):
    if root_obj is None:
        return
    for obj in (objects if objects is not None else _iter_object_tree(root_obj)):
        try:
            obj["witcher_layer_proxy_mesh"] = True
            obj["witcher_layer_visibility_kind"] = "proxy_mesh"
        except Exception:
            continue


def _set_hide_flags(obj, hidden):
    if obj.hide_viewport != hidden:
        obj.hide_viewport = hidden
    if obj.hide_render != hidden:
        obj.hide_render = hidden


def _clear_object_tree_proxy_mesh_tags(root_obj, objects=None):
    if root_obj is None:
        return
    for obj in (objects if objects is not None else _iter_object_tree(root_obj)):
        try:
            if "witcher_layer_proxy_mesh" in obj:
                del obj["witcher_layer_proxy_mesh"]
            if str(obj.get("witcher_layer_visibility_kind", "") or "").strip().lower() == "proxy_mesh":
                del obj["witcher_layer_visibility_kind"]
            if "witcher_sector_flags" in obj:
                del obj["witcher_sector_flags"]
            if "witcher_layer_engine_visible" in obj:
                del obj["witcher_layer_engine_visible"]
            _set_hide_flags(obj, False)
        except Exception:
            continue


def _apply_requested_proxy_helper_visibility(root_obj, kwargs=None):
    if root_obj is None:
        return
    hidden = bool((kwargs or {}).get("hide_proxy_meshes", False))
    for obj in _iter_object_tree(root_obj):
        try:
            if not bool(obj.get("witcher_apx_collision_proxy", False)):
                continue
            _set_hide_flags(obj, hidden)
        except Exception:
            continue


def _tag_object_tree_engine_visibility(root_obj, visible, kwargs=None, *, drawable_flags=None, sector_flags=None, objects=None):
    if root_obj is None:
        return
    visible = bool(visible)
    drawable_flags_text = _drawable_flags_display_value(drawable_flags) if drawable_flags is not None else None
    for obj in (objects if objects is not None else _iter_object_tree(root_obj)):
        try:
            obj["witcher_layer_engine_visible"] = visible
            if sector_flags is not None:
                obj["witcher_sector_flags"] = int(sector_flags)
            if drawable_flags_text is not None:
                obj["witcher_drawableFlags"] = drawable_flags_text
                obj["witcher_redkit_drawableFlags"] = drawable_flags_text
                obj["witcher_drawableFlags_has_DF_IsVisible"] = visible
            if _hide_engine_hidden_meshes_enabled(kwargs):
                _set_hide_flags(obj, not visible)
        except Exception:
            continue


def _tag_object_tree_drawable_shadows(root_obj, drawable_flags, component_type=""):
    default = str(component_type or "") in {
        "CDestructionComponent",
        "CDestructionSystemComponent",
    }
    casts_shadows = _drawable_flags_cast_shadows_from_value(
        drawable_flags,
        default=default,
    )
    local_only = _drawable_flags_local_only_from_value(drawable_flags)
    flags_text = _drawable_flags_display_value(drawable_flags) or "Unset"
    for obj in _iter_object_tree(root_obj):
        try:
            obj["witcher_redkit_drawableFlags"] = flags_text
            obj["witcher_drawableFlags_has_DF_CastShadows"] = casts_shadows
            obj["witcher_drawableFlags_local_lights_only"] = local_only
            if obj.type == "MESH":
                obj.visible_shadow = casts_shadows
        except Exception:
            continue


def _capture_previous_layer_object_ids(collection, owner_tag, fallback_to_all=False):
    if collection is None:
        return set()
    owner_tag = str(owner_tag or "").strip()
    tagged_ids = set()
    all_ids = set()
    for obj in list(getattr(collection, "all_objects", []) or []):
        obj_id = _object_identity(obj)
        if obj_id is None:
            continue
        all_ids.add(obj_id)
        try:
            if owner_tag and str(obj.get(_LAYER_IMPORT_OWNER_PROP, "") or "").strip() == owner_tag:
                tagged_ids.add(obj_id)
        except Exception:
            continue
    if tagged_ids:
        return tagged_ids
    if fallback_to_all:
        return all_ids
    return set()


def _object_parent_depth(obj):
    depth = 0
    current = getattr(obj, "parent", None)
    seen = set()
    while current is not None:
        current_id = _object_identity(current)
        if current_id in seen:
            break
        seen.add(current_id)
        depth += 1
        current = getattr(current, "parent", None)
    return depth


def _cleanup_captured_layer_objects(collection, object_ids):
    if collection is None or not object_ids:
        return 0
    captured_ids = {int(obj_id) for obj_id in object_ids if obj_id is not None}
    objects_to_remove = []
    for obj in list(getattr(collection, "all_objects", []) or []):
        obj_id = _object_identity(obj)
        if obj_id in captured_ids:
            objects_to_remove.append(obj)
    objects_to_remove.sort(key=_object_parent_depth, reverse=True)
    removed_count = 0
    for obj in objects_to_remove:
        if obj is None or getattr(obj, "name", None) not in bpy.data.objects:
            continue
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
            removed_count += 1
        except Exception:
            continue
    return removed_count


def _layer_reload_signature_changed(collection, kwargs):
    if collection is None:
        return False
    requested_mode = str(kwargs.get("_layer_import_mode_signature", "") or "").strip()
    previous_mode = str(collection.get("witcher_layer_load_mode", "") or "").strip()
    if requested_mode and previous_mode and requested_mode != previous_mode:
        return True
    requested_hash = str(kwargs.get("_layer_import_plan_hash", "") or "").strip()
    if requested_hash:
        previous_hash = str(collection.get("witcher_layer_import_plan_hash", "") or "").strip()
        if requested_hash != previous_hash:
            return True
    return False


def _ensure_layer_reload_tracking(collection, level_file, context, nearby_filter, kwargs):
    owner_tag = str(kwargs.get("_layer_import_owner") or _get_layer_import_owner_tag(level_file, context)).strip()
    if owner_tag:
        kwargs["_layer_import_owner"] = owner_tag
    if "_layer_import_previous_ids" in kwargs:
        return
    state = str(collection.get("witcher_layer_import_state", "") or "").strip().lower() if collection is not None else ""
    cleanup_existing = (
        nearby_filter is not None
        or state.startswith("proxy_")
        or _layer_reload_signature_changed(collection, kwargs)
    ) and not bool(kwargs.get("_layer_import_incremental"))
    if not cleanup_existing or collection is None:
        kwargs["_layer_import_previous_ids"] = set()
        return
    fallback_to_all = state in {"partial", "failed", "proxy_partial", "proxy_failed"}
    kwargs["_layer_import_previous_ids"] = _capture_previous_layer_object_ids(
        collection,
        owner_tag,
        fallback_to_all=fallback_to_all,
    )
    if kwargs["_layer_import_previous_ids"]:
        kwargs["_entity_import_disable_loaded_reuse"] = True


def _finalize_layer_reload_cleanup(collection, kwargs):
    previous_ids = kwargs.get("_layer_import_previous_ids")
    if not previous_ids:
        return 0
    removed_count = _cleanup_captured_layer_objects(collection, previous_ids)
    if removed_count:
        _invalidate_duplicate_root_index()
    kwargs["_layer_import_previous_ids"] = set()
    return removed_count


def _ensure_resolved_plan_reload_tracking(
    plan,
    collection,
    level_file,
    context,
    nearby_filter,
    dev_empty_only,
    kwargs,
):
    if not str(kwargs.get("_layer_import_plan_hash", "") or "").strip():
        kwargs["_layer_import_plan_hash"] = entity_import_plan_hash(plan)
    if kwargs.get("_layer_import_previous_ids"):
        return
    kwargs.pop("_layer_import_previous_ids", None)
    kwargs.pop("_layer_import_incremental", None)
    mode_signature = str(kwargs.get("_layer_import_mode_signature", "") or "").strip()
    if not mode_signature:
        mode_signature = _layer_load_mode_signature(dev_empty_only)
    if (
        not dev_empty_only
        and not _layer_reload_signature_changed(collection, kwargs)
        and _cached_plan_loaded_item_ids(collection, mode_signature)
    ):
        kwargs["_layer_import_incremental"] = True
    _ensure_layer_reload_tracking(collection, level_file, context, nearby_filter, kwargs)


def _find_level_collection(level_path, context=None):
    level_repo_path = _normalize_level_repo_path(level_path, context)
    level_abs_path = _normalize_repo_path(level_path)
    for collection in bpy.data.collections:
        stored_layer_path = collection.get("w2layer_path", "") or collection.get("level_path", "")
        stored_repo_path = _normalize_level_repo_path(stored_layer_path, context)
        stored_abs_path = _normalize_repo_path(collection.get("level_abs_path", ""))
        if stored_abs_path and stored_abs_path.lower() == level_abs_path.lower():
            return collection
        if stored_repo_path and stored_repo_path.lower() == level_repo_path.lower():
            return collection
    return None


def _ensure_level_collection(level_path, context=None):
    collection = _find_level_collection(level_path, context)
    level_repo_path = _normalize_level_repo_path(level_path, context)
    level_abs_path = _normalize_repo_path(level_path)
    if collection is None:
        level_name = os.path.basename(level_repo_path or level_abs_path) or "Level"
        collection = bpy.data.collections.new(level_name)
        scene = _get_scene(context)
        if scene is not None:
            scene.collection.children.link(collection)
    collection["w2layer_path"] = level_repo_path
    collection["level_path"] = level_repo_path
    collection["level_abs_path"] = level_abs_path
    return collection


def _activate_collection(context, collection):
    if collection is None:
        return False
    ctx = context or bpy.context
    view_layer = getattr(ctx, "view_layer", None)
    if view_layer is None:
        return False
    active_layer_collection = getattr(view_layer, "active_layer_collection", None)
    if getattr(active_layer_collection, "collection", None) == collection:
        return True
    layer_collection = recurLayerCollection(getattr(view_layer, "layer_collection", None), collection)
    if layer_collection is None:
        return False
    view_layer.active_layer_collection = layer_collection
    return True

def import_light(mesh, parent_transform = False):
    block = mesh.block
    light_data = block.packedObject
    if block.packedObjectType == Enums.BlockDataObjectType.PointLight:
        bpy.ops.object.light_add(type='POINT', radius=1, align='WORLD', location=(0, 0, 0), scale=(1, 1, 1))
        light_obj = bpy.context.selected_objects[:][0]
        light_obj.data.energy = light_data.brightness * 10
        light_obj.data.color[0] = light_data.color.Red/255
        light_obj.data.color[1] = light_data.color.Green/255
        light_obj.data.color[2] = light_data.color.Blue/255
        # do some custom val? #light_obj.data.color[3] = color.Value/255
        light_obj.data.shadow_soft_size = light_data.radius/255
        #set_blender_object_transform(light_obj, component.GetVariableByName('transform').EngineTransform)
        
    elif block.packedObjectType == Enums.BlockDataObjectType.SpotLight:
        bpy.ops.object.light_add(type='SPOT', radius=1, align='WORLD', location=(0, 0, 0), scale=(1, 1, 1))
        light_obj = bpy.context.selected_objects[:][0]
        light_obj.data.energy = light_data.brightness * 3
        light_obj.data.color[0] = light_data.color.Red/255
        light_obj.data.color[1] = light_data.color.Green/255
        light_obj.data.color[2] = light_data.color.Blue/255
        light_obj.data.shadow_soft_size = light_data.radius/255

        #light_obj.data.spot_blend = component.GetVariableByName('innerAngle').Value
        light_obj.data.spot_blend = 0
        light_obj.data.spot_size = light_data.outerAngle
        #light_obj.data.spot_size = component.GetVariableByName('softness').Value



    obj = light_obj
    if parent_transform:
        obj.parent = parent_transform

    if mesh.transform:
        obj.rotation_euler = (0,0,0)
        x, y, z = (
            radians(_transform_real(mesh.transform, "Yaw", 0.0)),
            radians(_transform_real(mesh.transform, "Pitch", 0.0)),
            radians(_transform_real(mesh.transform, "Roll", 0.0)),
        )
        orders =  ['XYZ', 'XZY', 'YXZ', 'YZX', 'ZXY', 'ZYX']
        mat = Euler((x, y, z), orders[2]).to_matrix().to_4x4()

        obj.matrix_world @= mat
        obj.location[0] = _transform_real(mesh.transform, "X", 0.0)
        obj.location[1] = _transform_real(mesh.transform, "Y", 0.0)
        obj.location[2] = _transform_real(mesh.transform, "Z", 0.0)

        if isinstance(mesh.transform, dict) or hasattr(mesh.transform, "Scale_x"):
            obj.scale[0] = _transform_real(mesh.transform, "Scale_x", 1.0)
            obj.scale[1] = _transform_real(mesh.transform, "Scale_y", 1.0)
            obj.scale[2] = _transform_real(mesh.transform, "Scale_z", 1.0)

    if mesh.matrix:
        try:
            log.info(obj.name)
            mat = Matrix()
            #log.info(mat)
            obj.matrix_world = obj.matrix_world @ mat
        except Exception:
            error_message = "ERROR MESH IMPORTER: Can't import: " + mesh.fbxPath()
            log.info(error_message)
    if mesh.translation:
        translation = _extract_vector_position(mesh.translation)
        if translation is not None:
            obj.location[0] = translation[0]
            obj.location[1] = translation[1]
            obj.location[2] = translation[2]
        
    if block.packedObjectType == Enums.BlockDataObjectType.SpotLight:
        # 90 to X in every spotlight
        rotation_euler = light_obj.rotation_euler
        rotation_euler.x += 1.5708  # 90 degrees in radians
        light_obj.rotation_euler = rotation_euler

#global repo_lookup_list

# import cProfile
# import pstats

def loadLevel(levelData, context = None, keep_lod_meshes:bool = False, **kwargs):
    #! profiler = cProfile.Profile()
    #! profiler.enable()

    target_collection = kwargs.pop("_level_target_collection", None)
    #keep_empty_lods = kwargs.get('keep_empty_lods', False)
    #keep_proxy_meshes = kwargs.get('keep_proxy_meshes', False)

    do_import_Mesh = kwargs.get('do_import_Mesh', True)
    do_import_Collision = kwargs.get('do_import_Collision', True)
    do_import_RigidBody = kwargs.get('do_import_RigidBody', True)
    dev_empty_only = bool(kwargs.get("_dev_empty_only", False))
    kwargs["_layer_import_profile"] = _new_layer_import_profile()

    if context == None:
        context = bpy.context
    nearby_filter = _get_nearby_import_filter(kwargs)
    nearby_stats = _get_nearby_import_stats(kwargs)
    # global repo_lookup_list
    # repo_lookup_list = defaultdict(list)
    # scene = bpy.context.scene
    # for o in scene.objects:
    #     if o.type != 'EMPTY':
    #         continue
    #     if len(o.name) > 4 and o.name[-4] != "." and 'repo_path' in o:
    #         repo_lookup_list[o['repo_path']].append(o)
    levelFile = levelData.layerNode
    kwargs["_redkit_source_context_path"] = levelFile
    level_version = getattr(levelData, "version", 999)
    mesh_uncook_path = kwargs.get("_mesh_uncook_path") or kwargs.get("mesh_uncook_path") or ""
    if not mesh_uncook_path:
        mesh_uncook_path = _derive_w2_uncook_root_from_level_path(levelFile, level_version)
    if mesh_uncook_path:
        kwargs["_mesh_uncook_path"] = mesh_uncook_path
        kwargs["mesh_uncook_path"] = mesh_uncook_path
    mesh_fbx_uncook_path = kwargs.get("_mesh_fbx_uncook_path") or kwargs.get("mesh_fbx_uncook_path")
    if target_collection is None:
        target_collection = _ensure_level_collection(levelFile, context)
    kwargs["_level_target_collection"] = target_collection
    mode_signature = str(kwargs.get("_layer_import_mode_signature", "") or "").strip()
    if not mode_signature:
        mode_signature = _layer_load_mode_signature(dev_empty_only)
    if (
        not dev_empty_only
        and not _layer_reload_signature_changed(target_collection, kwargs)
        and _cached_plan_loaded_item_ids(target_collection, mode_signature)
    ):
        kwargs["_layer_import_incremental"] = True
    _ensure_layer_reload_tracking(target_collection, levelFile, context, nearby_filter, kwargs)

    if import_isolation.needs_isolation_session(context):
        with import_isolation.isolated_import_session(
            context,
            target_collection,
            label=Path(_normalize_level_repo_path(levelFile, context)).stem or Path(levelFile).stem or "Level",
        ) as session:
            kwargs["_level_target_collection"] = target_collection
            result = loadLevel(levelData, session.context, keep_lod_meshes, **kwargs)
        _finalize_layer_reload_cleanup(target_collection, kwargs)
        _activate_collection(context, target_collection)
        return result

    errors = []
    progress_count = 0
    _log_layer_import_start(levelFile)
    _set_layer_import_state(target_collection, levelFile, "in_progress")
    _raise_if_layer_import_cancelled(kwargs)

    ready_to_import = True#checkLevel(levelData)

    #create collection lfor this level
    if ready_to_import:
        collection = target_collection
        if not import_isolation.is_isolated_import_context(context):
            _activate_collection(context, collection)
        if not dev_empty_only and (do_import_Mesh or do_import_Collision or do_import_RigidBody):
            _get_duplicate_root_index(_get_scene(context))

    #start level import
    try:
        if ready_to_import:
            if dev_empty_only:
                _raise_if_layer_import_cancelled(kwargs)
                resolve_started = time.time()
                plan_kwargs = dict(kwargs)
                resolved_plan = resolve_level_import_plan(levelData, context, keep_lod_meshes, **plan_kwargs)
                log.info(
                    "Resolved layer plan for %s: %d items in %.3f seconds",
                    levelFile,
                    int(resolved_plan.get("stats", {}).get("total", 0) or 0),
                    time.time() - resolve_started,
                )
                _ensure_resolved_plan_reload_tracking(
                    resolved_plan,
                    target_collection,
                    levelFile,
                    context,
                    nearby_filter,
                    True,
                    kwargs,
                )
                _log_entity_plan_unsupported(levelFile, resolved_plan)
                preflight_fatal, _ = _entity_import_plan_preflight(resolved_plan, deep=False)
                if preflight_fatal:
                    raise RuntimeError(
                        f"Entity plan is not materializable for {levelFile}: "
                        f"{_preview_list(preflight_fatal, limit=4)}"
                    )
                dev_target_collection = (
                    _get_active_collection(context)
                    if import_isolation.is_isolated_import_context(context)
                    else collection
                )
                progress_count = _import_plan_as_dev_empties(resolved_plan, dev_target_collection, kwargs)
                _finalize_layer_reload_cleanup(target_collection, kwargs)
                filtered_count = int(nearby_stats.get("filtered", 0) or 0)
                _log_layer_import_complete(levelFile, progress_count, errors)
                mode_signature = str(kwargs.get("_layer_import_mode_signature", "") or "").strip()
                if not mode_signature:
                    mode_signature = _layer_load_mode_signature(True)
                _set_layer_import_state(
                    target_collection,
                    levelFile,
                    "proxy_complete" if not errors and filtered_count <= 0 else "proxy_partial",
                    progress_count,
                    len(errors),
                    filtered_count,
                    nearby_filter=nearby_filter,
                    mode_signature=mode_signature,
                    plan_hash=kwargs.get("_layer_import_plan_hash"),
                )
                return {'FINISHED'}
            else:
                shared_plan = None
                try:
                    plan_options = dict(kwargs)
                    plan_options["context"] = context
                    plan_options["keep_lod_meshes"] = keep_lod_meshes
                    shared_plan = build_entity_import_plan(
                        levelData,
                        source_path=levelFile,
                        options=plan_options,
                    )
                except LayerImportCancelled:
                    raise
                except Exception as exc:
                    raise RuntimeError(f"Could not build entity import plan for {levelFile}: {exc}") from exc

                _ensure_resolved_plan_reload_tracking(
                    shared_plan,
                    target_collection,
                    levelFile,
                    context,
                    nearby_filter,
                    False,
                    kwargs,
                )
                _log_entity_plan_unsupported(levelFile, shared_plan)
                plan_preflight = _entity_import_plan_preflight(shared_plan, deep=False)
                preflight_fatal = plan_preflight[0]
                if not preflight_fatal:
                    import_target_collection = (
                        _get_active_collection(context)
                        if import_isolation.is_isolated_import_context(context)
                        else collection
                    )
                    result = materialize_entity_import_plan(
                        shared_plan,
                        context=context,
                        target_collection=import_target_collection,
                        options=kwargs,
                        layer_context={
                            "loaded_collection": target_collection,
                            "errors": errors,
                            "level_file": levelFile,
                        },
                        preflight=plan_preflight,
                    )
                    progress_count = len(result.materialized_item_ids)
                    if result.errors and not result.created_objects:
                        raise RuntimeError(
                            f"Entity plan materialization failed for {levelFile}: "
                            f"{_preview_list(result.errors, limit=4)}"
                        )
                    _finalize_layer_reload_cleanup(target_collection, kwargs)
                    filtered_count = int(nearby_stats.get("filtered", 0) or 0)
                    _log_layer_import_complete(levelFile, progress_count, result.errors)
                    _log_layer_import_profile_summary(levelFile, kwargs)
                    mode_signature = str(kwargs.get("_layer_import_mode_signature", "") or "").strip()
                    if not mode_signature:
                        mode_signature = _layer_load_mode_signature(False)
                    _set_layer_import_state(
                        target_collection,
                        levelFile,
                        "complete" if not result.errors and filtered_count <= 0 else "partial",
                        progress_count,
                        len(result.errors),
                        filtered_count,
                        nearby_filter=nearby_filter,
                        mode_signature=mode_signature,
                        plan_hash=kwargs.get("_layer_import_plan_hash"),
                    )
                    return {'FINISHED'}
                raise RuntimeError(
                    f"Entity plan is not materializable for {levelFile}: "
                    f"{_preview_list(preflight_fatal, limit=4)}"
                )

    except LayerImportCancelled:
        filtered_count = int(nearby_stats.get("filtered", 0) or 0)
        _set_layer_import_state(
            target_collection,
            levelFile,
            "proxy_partial" if dev_empty_only else "partial",
            progress_count,
            len(errors),
            filtered_count,
        )
        raise
    except Exception:
        filtered_count = int(nearby_stats.get("filtered", 0) or 0)
        _set_layer_import_state(
            target_collection,
            levelFile,
            "proxy_failed" if dev_empty_only else "failed",
            progress_count,
            max(1, len(errors)),
            filtered_count,
        )
        raise

    #! #################
    #!     #PROFILER
    #! #################
    #! profiler.disable()
    
    #! # Dump profiling data to file
    #! with open('profile_results.log', 'w') as f:
    #!     profiler.dump_stats(f.name)

    #! # Read profiling data from file and print to log file
    #! with open('log_file.txt', 'w') as log_file:
    #!     stats = pstats.Stats('profile_results.log', stream=log_file)
    #!     stats.sort_stats('cumulative')
    #!     stats.print_stats()
    
    return {'FINISHED'}


def loadLevelFromCachedPlan(level_file, plan_items, context=None, **kwargs):
    """Fast path for cached plan layer loads.

    Skips parsing the .w2l binary and re-resolving the import plan; instead
    consumes the plan items captured at scan-time (entry["items"] in the
    world layer cache). Supports dev-empty proxy loads and full loads for
    cached mesh/foliage/collision/rigid/cloth/light items.
    """
    target_collection = kwargs.pop("_level_target_collection", None)
    dev_empty_only = bool(kwargs.get("_dev_empty_only", False))

    if context is None:
        context = bpy.context

    nearby_filter = _get_nearby_import_filter(kwargs)
    nearby_stats = _get_nearby_import_stats(kwargs)

    if target_collection is None:
        target_collection = _ensure_level_collection(level_file, context)
    kwargs["_level_target_collection"] = target_collection
    mode_signature = str(kwargs.get("_layer_import_mode_signature", "") or "").strip()
    if not mode_signature:
        mode_signature = _layer_load_mode_signature(dev_empty_only)
    if (
        not dev_empty_only
        and not _layer_reload_signature_changed(target_collection, kwargs)
        and _cached_plan_loaded_item_ids(target_collection, mode_signature)
    ):
        kwargs["_layer_import_incremental"] = True
    _ensure_layer_reload_tracking(target_collection, level_file, context, nearby_filter, kwargs)

    if import_isolation.needs_isolation_session(context):
        with import_isolation.isolated_import_session(
            context,
            target_collection,
            label=Path(_normalize_level_repo_path(level_file, context)).stem or Path(level_file).stem or "Level",
        ) as session:
            kwargs["_level_target_collection"] = target_collection
            result = loadLevelFromCachedPlan(level_file, plan_items, session.context, **kwargs)
        _finalize_layer_reload_cleanup(target_collection, kwargs)
        _activate_collection(context, target_collection)
        return result

    errors = []
    progress_count = 0
    if not dev_empty_only:
        kwargs["_layer_import_profile"] = _new_layer_import_profile()
    _log_layer_import_start(level_file)
    _set_layer_import_state(target_collection, level_file, "in_progress")
    _raise_if_layer_import_cancelled(kwargs)

    if not import_isolation.is_isolated_import_context(context):
        _activate_collection(context, target_collection)

    try:
        _raise_if_layer_import_cancelled(kwargs)
        source_items = list(plan_items or [])
        if not dev_empty_only:
            source_items = cached_plan_filter_items_for_import_options(
                source_items,
                kwargs,
                context=context,
            )
            source_items = _maybe_group_cached_items_into_sector_instancers(source_items, kwargs)
            source_items = _ensure_cached_sector_group_hierarchy(source_items)
        filtered_items = _filter_cached_plan_items_by_proximity(
            source_items,
            nearby_filter,
            nearby_stats,
        )
        plan = {
            "mode": "cached_layer",
            "items": filtered_items,
            "stats": {"total": len(filtered_items), "filtered": 0, "by_kind": {}},
        }
        plan_preflight = _entity_import_plan_preflight(plan, deep=False)
        preflight_fatal = plan_preflight[0]
        if preflight_fatal:
            raise RuntimeError(
                f"Cached entity plan is not materializable for {level_file}: "
                f"{_preview_list(preflight_fatal, limit=4)}"
            )
        import_target_collection = (
            _get_active_collection(context)
            if import_isolation.is_isolated_import_context(context)
            else target_collection
        )
        if not dev_empty_only:
            result = materialize_entity_import_plan(
                plan,
                context=context,
                target_collection=import_target_collection,
                options=kwargs,
                layer_context={
                    "loaded_collection": target_collection,
                    "errors": errors,
                    "level_file": level_file,
                },
                preflight=plan_preflight,
            )
            progress_count = len(result.materialized_item_ids)
        else:
            progress_count = _import_plan_as_dev_empties(plan, import_target_collection, kwargs)

        _finalize_layer_reload_cleanup(target_collection, kwargs)
        filtered_count = int(nearby_stats.get("filtered", 0) or 0)
        _log_layer_import_complete(level_file, progress_count, errors)
        if not dev_empty_only:
            _log_layer_import_profile_summary(level_file, kwargs)
        complete_state = "proxy_complete" if dev_empty_only else "complete"
        partial_state = "proxy_partial" if dev_empty_only else "partial"
        _set_layer_import_state(
            target_collection,
            level_file,
            complete_state if not errors and filtered_count <= 0 else partial_state,
            progress_count,
            len(errors),
            filtered_count,
            nearby_filter=nearby_filter,
            mode_signature=mode_signature,
            plan_hash=kwargs.get("_layer_import_plan_hash"),
        )
    except LayerImportCancelled:
        filtered_count = int(nearby_stats.get("filtered", 0) or 0)
        _set_layer_import_state(
            target_collection,
            level_file,
            "proxy_partial" if dev_empty_only else "partial",
            progress_count,
            len(errors),
            filtered_count,
        )
        raise
    except Exception:
        filtered_count = int(nearby_stats.get("filtered", 0) or 0)
        _set_layer_import_state(
            target_collection,
            level_file,
            "proxy_failed" if dev_empty_only else "failed",
            progress_count,
            max(1, len(errors)),
            filtered_count,
        )
        raise

    return {'FINISHED'}


from bpy.types import Object, Mesh

_DEFERRED_MATERIAL_QUEUE = []
_DEFERRED_MATERIALS_PAUSED = False
_DEFERRED_MATERIAL_MAX_ATTEMPTS = 10
_DEFERRED_MATERIAL_ERROR_PROP = "witcher_materials_deferred_error"
_DEFERRED_MATERIAL_EXHAUSTED_PROP = "witcher_materials_retry_exhausted"
_DEFERRED_MATERIAL_BURST = False
_DEFERRED_MATERIAL_TICK_BUDGET = 0.3
_DEFERRED_MATERIAL_BURST_BUDGET = 1.5
_DEFERRED_MATERIAL_STATS = {"entries_done": 0, "objects_done": 0, "seconds": 0.0, "slowest_path": "", "slowest_seconds": 0.0}


def set_deferred_material_burst(enabled):
    global _DEFERRED_MATERIAL_BURST
    _DEFERRED_MATERIAL_BURST = bool(enabled)


def reset_deferred_material_stats():
    _DEFERRED_MATERIAL_STATS.update(
        {"entries_done": 0, "objects_done": 0, "seconds": 0.0, "slowest_path": "", "slowest_seconds": 0.0}
    )


def deferred_material_stats():
    return dict(_DEFERRED_MATERIAL_STATS)


def set_deferred_materials_paused(paused):
    global _DEFERRED_MATERIALS_PAUSED
    _DEFERRED_MATERIALS_PAUSED = bool(paused)
    if not _DEFERRED_MATERIALS_PAUSED:
        queue_missing_scene_materials()
        ensure_deferred_material_timer()


def _strip_win_prefix(path):
    p = str(path or "")
    return p[4:] if p.startswith("\\\\?\\") else p


def _deferred_material_key(path, embedded_chunk_index):
    path = os.path.normcase(os.path.normpath(_strip_win_prefix(path)))
    return path, embedded_chunk_index


def queue_deferred_mesh_materials(resolved_path, embedded_chunk_index, objs, repo_path=""):
    resolved_path = str(resolved_path or "")
    clean_repo_path = _strip_win_prefix(repo_path)
    identity_path = resolved_path or clean_repo_path
    if not identity_path:
        return
    key = _deferred_material_key(identity_path, embedded_chunk_index)
    mesh_objects = [o for o in objs or [] if getattr(o, "type", "") == 'MESH']
    names = list(dict.fromkeys(o.name for o in mesh_objects))
    if not names:
        return
    for obj in mesh_objects:
        obj.pop(_DEFERRED_MATERIAL_ERROR_PROP, None)
        obj.pop(_DEFERRED_MATERIAL_EXHAUSTED_PROP, None)
        obj["witcher_materials_pending"] = True
    for entry in _DEFERRED_MATERIAL_QUEUE:
        if _deferred_material_key(entry[0], entry[1]) != key:
            continue
        entry[2][:] = list(dict.fromkeys([*entry[2], *names]))
        if not entry[3] and clean_repo_path:
            entry[3] = clean_repo_path
        if not _DEFERRED_MATERIALS_PAUSED:
            ensure_deferred_material_timer()
        return
    _DEFERRED_MATERIAL_QUEUE.append([identity_path, embedded_chunk_index, names, clean_repo_path, 0])
    if not _DEFERRED_MATERIALS_PAUSED:
        ensure_deferred_material_timer()


def _resolve_deferred_mesh_path(path, repo_path):
    candidate = _strip_win_prefix(path)
    if candidate and os.path.isabs(candidate) and os.path.exists(win_safe_path(candidate)):
        return candidate
    fallback = str(repo_path or "") or (candidate if not os.path.isabs(candidate) else "")
    if fallback:
        try:
            resolved = repo_file(fallback)
            if resolved and os.path.exists(win_safe_path(resolved)):
                return resolved
        except Exception:
            pass
    return ""


def deferred_material_queue_size():
    return sum(len(entry[2]) for entry in _DEFERRED_MATERIAL_QUEUE)


def deferred_material_entry_count():
    return len(_DEFERRED_MATERIAL_QUEUE)


def replace_deferred_queue_objects(old_names, new_name):
    old = {str(n) for n in old_names or [] if n}
    new_name = str(new_name or "")
    if not old or not new_name:
        return
    for entry in _DEFERRED_MATERIAL_QUEUE:
        if old.intersection(entry[2]):
            entry[2][:] = list(dict.fromkeys(new_name if name in old else name for name in entry[2]))


_DEFERRED_MATERIAL_DONE = 0


def _tag_view3d_redraw():
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
    except Exception:
        pass


def _set_deferred_status(text):
    try:
        bpy.context.workspace.status_text_set(text)
    except Exception:
        pass


def _normalized_deferred_source_path(path):
    return os.path.normcase(os.path.normpath(_strip_win_prefix(path))).replace("/", "\\")


def _deferred_object_source_paths(obj):
    values = [
        obj.get("witcher_resolved_mesh_path", ""),
        obj.get("repo_path", ""),
        obj.parent.get("repo_path", "") if obj.parent is not None else "",
    ]
    # Shared materials are a fallback; object paths identify the source mesh.
    if not any(values):
        values = [
            mat.get("w3_source_mesh_path", "")
            for mat in obj.data.materials
            if mat is not None
        ]
    return {_normalized_deferred_source_path(value) for value in values if value}


def _find_deferred_material_targets(path, repo_path, chunk_idx):
    source_keys = {
        _normalized_deferred_source_path(value)
        for value in (path, repo_path)
        if value
    }
    if not source_keys:
        return []
    targets = []
    for obj in bpy.data.objects:
        if getattr(obj, "type", "") != 'MESH' or getattr(obj, "data", None) is None:
            continue
        if not obj.get("witcher_materials_pending", False):
            continue
        obj_chunk = obj.get("witcher_embedded_cmesh_chunk_index")
        if obj_chunk is None and obj.parent is not None:
            obj_chunk = obj.parent.get("witcher_embedded_cmesh_chunk_index")
        if chunk_idx is not None and obj_chunk != chunk_idx:
            continue
        if source_keys.intersection(_deferred_object_source_paths(obj)):
            targets.append(obj)
    return targets


def _exhaust_deferred_material_entry(entry, objs, message):
    if entry[4] < _DEFERRED_MATERIAL_MAX_ATTEMPTS:
        return False
    _DEFERRED_MATERIAL_QUEUE.pop(0)
    for obj in objs:
        obj[_DEFERRED_MATERIAL_ERROR_PROP] = str(message)
        obj[_DEFERRED_MATERIAL_EXHAUSTED_PROP] = True
    log.error(
        "Deferred materials stopped after %d attempts for %s. Reloading the file retries automatically.",
        entry[4],
        message,
    )
    return True


def _deferred_material_tick():
    global _DEFERRED_MATERIAL_DONE
    if _DEFERRED_MATERIALS_PAUSED:
        return 0.5
    from ..materials.nodes.domain import suspend_witcher_include_layout
    started = time.perf_counter()
    budget = _DEFERRED_MATERIAL_BURST_BUDGET if _DEFERRED_MATERIAL_BURST else _DEFERRED_MATERIAL_TICK_BUDGET
    retry_delay = False
    with suspend_witcher_include_layout():
        while _DEFERRED_MATERIAL_QUEUE and time.perf_counter() - started < budget:
            entry_started = time.perf_counter()
            entry = _DEFERRED_MATERIAL_QUEUE[0]
            path, chunk_idx, names = entry[0], entry[1], entry[2]
            repo_path = entry[3] if len(entry) > 3 else ""
            objs = [
                o for o in (bpy.data.objects.get(n) for n in names)
                if o is not None and getattr(o, "type", "") == 'MESH'
            ]
            if not objs:
                objs = _find_deferred_material_targets(path, repo_path, chunk_idx)
            if not objs:
                _DEFERRED_MATERIAL_QUEUE.pop(0)
                continue
            resolved = _resolve_deferred_mesh_path(path, repo_path)
            if not resolved:
                entry[4] += 1
                if entry[4] == 1 or entry[4] % 10 == 0:
                    log.error("Deferred materials: mesh not found for %s (repo %s); retry %d", path, repo_path or "<none>", entry[4])
                if _exhaust_deferred_material_entry(entry, objs, repo_path or path):
                    continue
                _DEFERRED_MATERIAL_QUEUE.append(_DEFERRED_MATERIAL_QUEUE.pop(0))
                retry_delay = True
                break
            try:
                loaded = import_mesh.import_mesh_materials(resolved, objs, embedded_cmesh_chunk_index=chunk_idx)
            except Exception:
                loaded = False
                log.exception("Deferred material load failed for %s; keeping it queued", resolved)
            ready = loaded is not False and all(
                import_mesh.witcher_mesh_materials_ready(obj, repair_vertex_color=True)
                for obj in objs
            )
            if not ready:
                entry[4] += 1
                if entry[4] == 1 or entry[4] % 10 == 0:
                    log.error("Deferred materials failed validation for %s; retry %d", repo_path or resolved, entry[4])
                if _exhaust_deferred_material_entry(entry, objs, repo_path or resolved):
                    continue
                _DEFERRED_MATERIAL_QUEUE.append(_DEFERRED_MATERIAL_QUEUE.pop(0))
                retry_delay = True
                break
            _DEFERRED_MATERIAL_QUEUE.pop(0)
            for obj in objs:
                obj.pop("witcher_materials_pending", None)
                obj.pop(_DEFERRED_MATERIAL_ERROR_PROP, None)
                obj.pop(_DEFERRED_MATERIAL_EXHAUSTED_PROP, None)
            _DEFERRED_MATERIAL_DONE += 1
            entry_seconds = time.perf_counter() - entry_started
            _DEFERRED_MATERIAL_STATS["entries_done"] += 1
            _DEFERRED_MATERIAL_STATS["objects_done"] += len(objs)
            _DEFERRED_MATERIAL_STATS["seconds"] += entry_seconds
            if entry_seconds > _DEFERRED_MATERIAL_STATS["slowest_seconds"]:
                _DEFERRED_MATERIAL_STATS["slowest_seconds"] = entry_seconds
                _DEFERRED_MATERIAL_STATS["slowest_path"] = str(repo_path or resolved or path)
            if entry_seconds >= 1.0:
                log.info(
                    "[deferred-material-profile] %s took %.3fs (%d objects)",
                    repo_path or resolved or path,
                    entry_seconds,
                    len(objs),
                )
    remaining = len(_DEFERRED_MATERIAL_QUEUE)
    _tag_view3d_redraw()
    if not remaining:
        recovered = queue_missing_scene_materials()
        if recovered:
            log.warning("Deferred material validation automatically recovered %d missed batches", recovered)
            remaining = len(_DEFERRED_MATERIAL_QUEUE)
    if not remaining:
        total = _DEFERRED_MATERIAL_DONE
        _DEFERRED_MATERIAL_DONE = 0
        failed = sum(
            1 for obj in bpy.data.objects
            if getattr(obj, "type", "") == 'MESH' and obj.get(_DEFERRED_MATERIAL_EXHAUSTED_PROP, False)
        )
        if failed:
            _set_deferred_status(f"Witcher: material streaming failed for {failed} meshes")
            log.error("Deferred material streaming ended with %d failed meshes; see object error properties", failed)
        else:
            _set_deferred_status(None)
            log.info("Deferred material streaming complete (%d meshes)", total)
        return None
    target_count = deferred_material_queue_size()
    _set_deferred_status(f"Witcher: streaming materials — {target_count} meshes remaining")
    if _DEFERRED_MATERIAL_DONE % 100 == 0:
        log.info("Deferred material streaming: %d done, %d targets remaining", _DEFERRED_MATERIAL_DONE, target_count)
    if retry_delay:
        return 0.5
    return 0.01 if _DEFERRED_MATERIAL_BURST else 0.05


def _deferred_material_source_for_object(obj):
    mat = next((
        candidate for candidate in obj.data.materials
        if candidate is not None and candidate.get("w3_source_material_name") is not None
    ), None)
    src = str(
        obj.get("witcher_resolved_mesh_path", "")
        or obj.get("repo_path", "")
        or (obj.parent.get("repo_path", "") if obj.parent is not None else "")
        or (mat.get("w3_source_mesh_path", "") if mat is not None else "")
        or ""
    )
    if not src:
        return None
    chunk = obj.get("witcher_embedded_cmesh_chunk_index")
    if chunk is None and obj.parent is not None:
        chunk = obj.parent.get("witcher_embedded_cmesh_chunk_index")
    return (src, int(chunk) if chunk is not None else None)


def _queue_deferred_material_groups(groups):
    queued = 0
    for (src, chunk), objs in groups.items():
        src_clean = _strip_win_prefix(src)
        try:
            resolved = repo_file(src_clean)
        except Exception:
            resolved = src_clean
        before = len(_DEFERRED_MATERIAL_QUEUE)
        queue_deferred_mesh_materials(resolved, chunk, objs, repo_path=src_clean)
        queued += len(_DEFERRED_MATERIAL_QUEUE) - before
    return queued


def queue_deferred_materials_for_objects(objects):
    groups = {}
    for obj in objects or []:
        if getattr(obj, "type", "") != 'MESH':
            continue
        if import_mesh.witcher_mesh_materials_ready(obj, repair_vertex_color=False):
            continue
        key = _deferred_material_source_for_object(obj)
        if key is None:
            continue
        groups.setdefault(key, []).append(obj)
    return _queue_deferred_material_groups(groups)


def queue_missing_scene_materials():
    groups = {}
    for obj in bpy.data.objects:
        if getattr(obj, "type", "") != 'MESH':
            continue
        if not obj.get("witcher_materials_pending", False):
            continue
        if obj.get(_DEFERRED_MATERIAL_EXHAUSTED_PROP, False):
            continue
        ready = import_mesh.witcher_mesh_materials_ready(obj, repair_vertex_color=True)
        if ready:
            obj.pop("witcher_materials_pending", None)
            continue
        key = _deferred_material_source_for_object(obj)
        if key is None:
            continue
        groups.setdefault(key, []).append(obj)
    return _queue_deferred_material_groups(groups)


def ensure_deferred_material_timer():
    if not _DEFERRED_MATERIAL_QUEUE:
        return
    try:
        if not bpy.app.timers.is_registered(_deferred_material_tick):
            bpy.app.timers.register(_deferred_material_tick, first_interval=0.5)
    except Exception:
        log.exception("Could not start deferred material timer")


def _scan_loaded_deferred_materials():
    try:
        queue_missing_scene_materials()
        ensure_deferred_material_timer()
    except Exception:
        log.exception("Could not recover deferred materials after loading the scene")
    return None


@persistent
def _resume_deferred_materials_on_load(_filepath):
    global _DEFERRED_MATERIAL_DONE, _DEFERRED_MATERIALS_PAUSED
    _DEFERRED_MATERIAL_QUEUE.clear()
    _DEFERRED_MATERIAL_DONE = 0
    _DEFERRED_MATERIALS_PAUSED = False
    try:
        from ..materials.material import invalidate_find_material_index
        invalidate_find_material_index()
    except Exception:
        pass
    for obj in getattr(bpy.data, "objects", ()):
        if getattr(obj, "type", "") == 'MESH' and obj.get("witcher_materials_pending", False):
            obj.pop(_DEFERRED_MATERIAL_ERROR_PROP, None)
            obj.pop(_DEFERRED_MATERIAL_EXHAUSTED_PROP, None)
    try:
        if not bpy.app.timers.is_registered(_scan_loaded_deferred_materials):
            bpy.app.timers.register(_scan_loaded_deferred_materials, first_interval=0.25)
    except Exception:
        _scan_loaded_deferred_materials()


def register_deferred_material_load_handler():
    if _resume_deferred_materials_on_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_resume_deferred_materials_on_load)
    _resume_deferred_materials_on_load(None)


def unregister_deferred_material_load_handler():
    if _resume_deferred_materials_on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_resume_deferred_materials_on_load)
    for callback in (_scan_loaded_deferred_materials, _deferred_material_tick):
        try:
            if bpy.app.timers.is_registered(callback):
                bpy.app.timers.unregister(callback)
        except Exception:
            pass
    _DEFERRED_MATERIAL_QUEUE.clear()
    _set_deferred_status(None)


def repo_in_scene(dct, path):
    if path in dct.keys():
        return True
    else:
        return False

def _has_blender_numeric_suffix(name: str) -> bool:
    return bool(name) and len(name) > 4 and name[-4] == "." and name[-3:].isdigit()


def _is_duplicate_root_candidate(obj, repo_path=None):
    if not _is_live_blender_object(obj):
        return False
    try:
        obj_type = getattr(obj, "type", "")
        obj_name = getattr(obj, "name", "")
        obj_repo_path = str(obj.get("repo_path", "") or "").strip()
    except ReferenceError:
        return False
    except Exception:
        return False
    if obj_type != 'EMPTY':
        return False
    if not obj_name:
        return False
    if not obj_repo_path:
        return False
    if repo_path is not None and obj_repo_path != repo_path:
        return False
    return True


def _prefer_duplicate_root(current_obj, candidate_obj):
    if not _is_duplicate_root_candidate(candidate_obj):
        return current_obj
    if not _is_duplicate_root_candidate(current_obj):
        return candidate_obj
    try:
        current_primary = not _has_blender_numeric_suffix(getattr(current_obj, "name", ""))
        candidate_primary = not _has_blender_numeric_suffix(getattr(candidate_obj, "name", ""))
    except ReferenceError:
        return candidate_obj
    if candidate_primary and not current_primary:
        return candidate_obj
    return current_obj


def _rebuild_duplicate_root_index(scene=None):
    scene = scene or _get_scene()
    roots = {}
    if scene is not None:
        for obj in scene.objects:
            if not _is_duplicate_root_candidate(obj):
                continue
            repo_path = str(obj.get("repo_path", "") or "").strip()
            roots[repo_path] = _prefer_duplicate_root(roots.get(repo_path), obj)
    _REPO_DUPLICATE_CACHE["scene_key"] = _scene_identity(scene)
    _REPO_DUPLICATE_CACHE["object_count"] = len(scene.objects) if scene is not None else -1
    _REPO_DUPLICATE_CACHE["roots"] = roots
    _REPO_DUPLICATE_CACHE["subtrees"] = {}
    return roots


def _record_duplicate_root_subtree(root_obj, subtree):
    root_id = _object_identity(root_obj)
    if root_id is None or not subtree:
        return
    ordered = [root_obj] + [obj for obj in subtree if obj is not root_obj]
    _REPO_DUPLICATE_CACHE["subtrees"][root_id] = ordered


def _duplicate_root_subtree(source_root):
    root_id = _object_identity(source_root)
    cached = _REPO_DUPLICATE_CACHE["subtrees"].get(root_id)
    if cached is not None:
        ids = {root_id}
        valid = True
        for obj in cached:
            if not _is_live_blender_object(obj):
                valid = False
                break
            ids.add(_object_identity(obj))
        if valid:
            for obj in cached[1:]:
                if _object_identity(getattr(obj, "parent", None)) not in ids:
                    valid = False
                    break
        if valid:
            return list(cached)
        _REPO_DUPLICATE_CACHE["subtrees"].pop(root_id, None)
    subtree = [source_root] + list(getattr(source_root, "children_recursive", None) or [])
    _REPO_DUPLICATE_CACHE["subtrees"][root_id] = list(subtree)
    return subtree


def _get_duplicate_root_index(scene=None):
    scene = scene or _get_scene()
    scene_key = _scene_identity(scene)
    object_count = len(scene.objects) if scene is not None else -1
    if _REPO_DUPLICATE_CACHE["scene_key"] != scene_key:
        return _rebuild_duplicate_root_index(scene)
    cached_object_count = int(_REPO_DUPLICATE_CACHE.get("object_count", -1))
    if object_count >= 0 and cached_object_count >= 0 and object_count < cached_object_count:
        return _rebuild_duplicate_root_index(scene)
    roots = _REPO_DUPLICATE_CACHE.get("roots") or {}
    _REPO_DUPLICATE_CACHE["object_count"] = max(cached_object_count, object_count)
    return roots


def _touch_duplicate_root_index(scene=None):
    scene = scene or _get_scene()
    if _REPO_DUPLICATE_CACHE["scene_key"] == _scene_identity(scene):
        object_count = len(scene.objects) if scene is not None else -1
        cached_object_count = int(_REPO_DUPLICATE_CACHE.get("object_count", -1))
        _REPO_DUPLICATE_CACHE["object_count"] = max(cached_object_count, object_count)


def _record_duplicate_root(obj, scene=None, subtree=None):
    if subtree is not None:
        _record_duplicate_root_subtree(obj, subtree)
    scene = scene or _get_scene()
    if scene is None:
        return
    if _REPO_DUPLICATE_CACHE["scene_key"] != _scene_identity(scene):
        _rebuild_duplicate_root_index(scene)
        if subtree is not None:
            _record_duplicate_root_subtree(obj, subtree)
        return
    object_count = len(scene.objects)
    cached_object_count = int(_REPO_DUPLICATE_CACHE.get("object_count", -1))
    _REPO_DUPLICATE_CACHE["object_count"] = max(cached_object_count, object_count)
    if not _is_duplicate_root_candidate(obj):
        return
    repo_path = str(obj.get("repo_path", "") or "").strip()
    current_obj = _REPO_DUPLICATE_CACHE["roots"].get(repo_path)
    _REPO_DUPLICATE_CACHE["roots"][repo_path] = _prefer_duplicate_root(current_obj, obj)


def _remap_object_reference(owner, attr_name, clone_by_id):
    if owner is None or not hasattr(owner, attr_name):
        return
    try:
        current_value = getattr(owner, attr_name)
    except Exception:
        return
    clone_value = clone_by_id.get(_object_identity(current_value))
    if clone_value is None:
        return
    try:
        setattr(owner, attr_name, clone_value)
    except Exception:
        return


def _clone_duplicate_hierarchy(source_root, target_collection=None, *, remap_links=True, collect=None):
    if not _is_duplicate_root_candidate(source_root):
        return None
    target_collection = target_collection or _get_active_collection()
    if target_collection is None:
        return None

    clone_pairs = []
    clone_by_id = {}
    source_basis_matrices = {}
    source_parent_inverses = {}
    try:
        source_objects = _duplicate_root_subtree(source_root)
    except ReferenceError:
        return None
    for source_obj in source_objects:
        source_id = _object_identity(source_obj)
        try:
            # Use basis to avoid same-tick stale local transforms.
            source_basis_matrices[source_id] = source_obj.matrix_basis.copy()
            source_parent_inverses[source_id] = source_obj.matrix_parent_inverse.copy()
        except Exception:
            pass
        clone_obj = source_obj.copy()
        target_collection.objects.link(clone_obj)
        clone_pairs.append((source_obj, clone_obj))
        clone_by_id[source_id] = clone_obj

    for source_obj, clone_obj in clone_pairs:
        clone_parent = clone_by_id.get(_object_identity(getattr(source_obj, "parent", None)))
        clone_obj.parent = clone_parent
        local_matrix = source_basis_matrices.get(_object_identity(source_obj))
        if local_matrix is not None:
            _set_object_local_matrix_direct(
                clone_obj,
                local_matrix,
                source_parent_inverses.get(_object_identity(source_obj)),
            )

    if remap_links:
        for _source_obj, clone_obj in clone_pairs:
            for modifier in getattr(clone_obj, "modifiers", []):
                for attr_name in ("object", "mirror_object", "offset_object"):
                    _remap_object_reference(modifier, attr_name, clone_by_id)
            for constraint in getattr(clone_obj, "constraints", []):
                for attr_name in ("target", "space_object"):
                    _remap_object_reference(constraint, attr_name, clone_by_id)

    new_root = clone_by_id.get(_object_identity(source_root))
    if new_root is None:
        return None

    identity = Matrix.Identity(4)
    new_root.parent = None
    _set_object_local_matrix_direct(new_root, identity)
    new_root.location[0] = 0
    new_root.location[1] = 0
    new_root.location[2] = 0
    new_root.scale[0] = 1
    new_root.scale[1] = 1
    new_root.scale[2] = 1
    for source_obj, clone_obj in clone_pairs:
        if clone_obj == new_root:
            continue
        source_id = _object_identity(source_obj)
        local_matrix = source_basis_matrices.get(source_id)
        if local_matrix is not None:
            _set_object_local_matrix_direct(
                clone_obj,
                local_matrix,
                source_parent_inverses.get(source_id),
            )
    if collect is not None:
        collect.extend(clone_obj for _source_obj, clone_obj in clone_pairs)
    return new_root


def check_if_empty_already_in_scene(repo_path, *, fast_static_clone=False, collect=None):
    scene = _get_scene()
    repo_path = str(repo_path or "").strip()
    if not repo_path:
        return False

    start_time1 = time.time()
    root_index = _get_duplicate_root_index(scene)
    source_root = root_index.get(repo_path)
    # Rebuild only stale cache hits.
    if source_root is not None and not _is_duplicate_root_candidate(source_root, repo_path):
        source_root = _rebuild_duplicate_root_index(scene).get(repo_path)
    if source_root is None:
        return False

    log.debug('Check Mesh found in %f seconds.', time.time() - start_time1)
    start_time2 = time.time()
    new_obj = _clone_duplicate_hierarchy(
        source_root,
        _get_active_collection(),
        remap_links=not bool(fast_static_clone),
        collect=collect,
    )
    if new_obj is None:
        return False
    _touch_duplicate_root_index(scene)
    log.debug('Check Mesh Finished importing in %f seconds.', time.time() - start_time2)
    return new_obj

def check_if_mesh_already_in_scene(repo_path):

    start_time1 = time.time()
    # name = Path(repo_path).stem+"_Mesh_lod0"
    # try:
    #     o = bpy.context.scene.objects[name]
    # except Exception as e:
    #     try:
    #         name = Path(repo_path).stem+"_Mesh"
    #         o = bpy.context.scene.objects[name]
    #     except Exception as e:
    #         return False
    # #else:
    for o in bpy.context.scene.objects:
        if o.type != 'MESH':
            continue
        if o.name[-4] != "." and 'repo_path' in o and o['repo_path'] == repo_path:
            log.info('Check Mesh found in %f seconds.', time.time() - start_time1)
            start_time2 = time.time()
            #log.info("COPYING", o['repo_path'])
            new_obj = o.copy()
            #new_obj.data = o.data.copy()
            #new_obj.animation_data_clear()
            bpy.context.collection.objects.link(new_obj)

            # new_obj.rotation_euler[0] = 0
            # new_obj.rotation_euler[1] = 0
            # new_obj.rotation_euler[2] = 0
            # new_obj.rotation_euler = (0,0,0)
            x, y, z = (radians(0), radians(0), radians(0))
            mat = Euler((x, y, z)).to_matrix().to_4x4()
            new_obj.matrix_world = mat
            new_obj.matrix_local = mat
            new_obj.matrix_basis = mat

            new_obj.location[0] = 0
            new_obj.location[1] = 0
            new_obj.location[2] = 0
            new_obj.scale[0] = 1
            new_obj.scale[1] = 1
            new_obj.scale[2] = 1
            new_obj.parent = None
            log.info('Check Mesh Finished importing in %f seconds.', time.time() - start_time2)
            return new_obj
    return False

def _import_foliage_mesh(mesh: meshPath):
    """Import a foliage tree mesh using SRT (SpeedTree) if available, FBX as fallback."""
    srt_status = get_srt_addon_status()
    if srt_status["enabled"]:
        try:
            srt_path = repo_file(mesh.meshName)
            if srt_path and os.path.exists(srt_path):
                from ..ui.ui_file_browser import (
                    _export_srt_textures_for_import,
                    _prepare_srt_lod0_json,
                    _snapshot_srt_import_state,
                    _flatten_srt_import_collections,
                )
                from .. import get_all_addon_prefs
                context = bpy.context
                prefs = get_all_addon_prefs(context)
                use_custom_grouping = bool(getattr(prefs, "ab_srt_custom_grouping", True))
                lod0_only = bool(getattr(prefs, "ab_srt_lod0_only", True))

                step_started = time.perf_counter()
                srt_snapshot = _snapshot_srt_import_state(context) if use_custom_grouping else {}
                tex_stats = _export_srt_textures_for_import(
                    context, srt_path, mesh.meshName, loadmods=False,
                )
                import_path = tex_stats.get("import_path") or srt_path
                if lod0_only:
                    import_path = _prepare_srt_lod0_json(import_path)
                prep_seconds = time.perf_counter() - step_started
                from ..cloth.importer import surgical_external_joins
                step_started = time.perf_counter()
                with surgical_external_joins():
                    result = getattr(bpy.ops, "import").srt_json(filepath=import_path)
                op_seconds = time.perf_counter() - step_started
                if 'FINISHED' in result:
                    step_started = time.perf_counter()
                    if use_custom_grouping:
                        _flatten_srt_import_collections(context, import_path, srt_snapshot)
                    flatten_seconds = time.perf_counter() - step_started
                    if prep_seconds + op_seconds + flatten_seconds >= 0.25:
                        log.info(
                            "[foliage] srt %s: prep %.2fs, import %.2fs, flatten %.2fs",
                            os.path.basename(srt_path),
                            prep_seconds,
                            op_seconds,
                            flatten_seconds,
                        )
                    return
                log.warning("SRT import failed for %s, falling back to FBX", mesh.meshName)
            else:
                log.warning("SRT file not found: %s, falling back to FBX", mesh.meshName)
        except Exception as e:
            log.warning("SRT import error for %s: %s, falling back to FBX", mesh.meshName, e)
    # Fallback to FBX
    bpy.ops.import_scene.fbx(filepath=mesh.fbxPath())


def import_single_mesh(mesh:meshPath, errors, parent_transform = False, keep_lod_meshes = False, version = 999, **kwargs):
    _raise_if_layer_import_cancelled(kwargs)
    mesh_started = time.perf_counter()
    try:
        requested_version = int(version)
    except Exception:
        requested_version = 999
    if requested_version == 999:
        version = _mesh_cr2w_version(mesh, kwargs.get("cr2w_version", requested_version))
    else:
        version = requested_version
    mesh_name = _mesh_repo_path(mesh)
    if not mesh_name:
        label = str(getattr(mesh, "name", "") or getattr(mesh, "type", "") or "mesh")
        message = f"Mesh import has an empty mesh path ({label})"
        log.error(message)
        _append_import_error(errors, message)
        raise ValueError(message)
    try:
        mesh.meshName = mesh_name
    except Exception:
        pass
    embedded_cmesh_chunk_index = _embedded_cmesh_chunk_index(mesh)
    scene_repo_key = _mesh_scene_repo_key(mesh_name, embedded_cmesh_chunk_index)
    use_fbx = (
        get_use_fbx_repo(bpy.context)
        and embedded_cmesh_chunk_index is None
        and not mesh_name.lower().endswith(".reddest")
    )
    import_seconds = 0.0
    finalize_seconds = 0.0
    transform_seconds = 0.0
    backend = "reuse"
    reused_existing = False

    reuse_clones = []
    obj = check_if_empty_already_in_scene(
        scene_repo_key,
        fast_static_clone=bool(kwargs.get("_cached_plan_fast_static_clone", False)),
        collect=reuse_clones,
    )
    # if keep_lod_meshes:
    #     obj = check_if_empty_already_in_scene(mesh.meshName)
    # else:
    #     obj = check_if_mesh_already_in_scene(mesh.meshName)
    #obj = False
    if not obj:
        resolved_cr2w_path = None
        if getattr(mesh, "type", "") != "mesh_foliage" and not use_fbx:
            resolved_cr2w_path = _existing_mesh_path_from_explicit_root(mesh_name, mesh, version)
            if not resolved_cr2w_path:
                try:
                    resolved_cr2w_path = repo_file(mesh_name, version)
                except Exception as exc:
                    message = f"Mesh import path could not be resolved {mesh_name}: {exc}"
                    log.error(message)
                    _append_import_error(errors, message)
                    raise
            if not resolved_cr2w_path or not os.path.exists(win_safe_path(resolved_cr2w_path)):
                message = f"Mesh file does not exist: {mesh_name} -> {resolved_cr2w_path}"
                log.error(message)
                _append_import_error(errors, message)
                raise FileNotFoundError(message)
        for prev_selected in bpy.context.selected_objects:
            prev_selected.select_set(False)
        pre_selected_ids = set()
        obj = bpy.data.objects.new("Empty", None)
        obj.empty_display_type = 'PLAIN_AXES'
        (_get_active_collection() or bpy.context.scene.collection).objects.link(obj)
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        defer_mats = False
        try:
            import_started = time.perf_counter()
            if getattr(mesh, "type", "") == "mesh_foliage":
                backend = "foliage"
                _import_foliage_mesh(mesh)
            else:
                if use_fbx and os.path.exists(mesh.fbxPath()):
                    backend = "fbx"
                    fbx_util.importFbx(mesh.fbxPath(),mesh.fileName(),mesh.fileName(), keep_lod_meshes=keep_lod_meshes)
                elif not use_fbx:
                    backend = "cr2w"
                    defer_mats = bool(kwargs.get("defer_mesh_materials"))
                    imported_meshes, _imported_armatures = import_mesh.import_mesh(
                        resolved_cr2w_path,
                        do_import_mats=not defer_mats,
                        keep_lod_meshes=keep_lod_meshes,
                        keep_empty_lods=kwargs.get('keep_empty_lods', False),
                        keep_proxy_meshes=kwargs.get('keep_proxy_meshes', False),
                        embedded_cmesh_chunk_index=embedded_cmesh_chunk_index,
                    )
                    if defer_mats:
                        # Queue before wrapper/finalization can replace the geometry.
                        queue_deferred_mesh_materials(
                            resolved_cr2w_path,
                            embedded_cmesh_chunk_index,
                            imported_meshes,
                            repo_path=mesh_name,
                        )
                else:
                    backend = "fallback_cube"
                    log.warning("Can't find FBX file %s", mesh.fbxPath())
                    bpy.ops.mesh.primitive_cube_add()
                    objs = bpy.context.selected_objects[:]
                    objs[0].color = (0,0,1,1)
                    objs[0].name = "ERROR_CUBE"
                    err_mat = bpy.data.materials.new("ERROR_CUBE_MAT")
                    err_mat.use_nodes = True
                    principled = err_mat.node_tree.nodes['Principled BSDF']
                    principled.inputs['Base Color'].default_value = (0,0,1,1)
                    objs[0].data.materials.append(err_mat)
            import_seconds = time.perf_counter() - import_started

        except Exception:
            log.exception("Problem importing mesh %s", mesh.meshName)
            raise
        try:
            finalize_started = time.perf_counter()

            objs = [
                subobj
                for subobj in bpy.context.selected_objects[:]
                if subobj.as_pointer() not in pre_selected_ids
            ]
            if obj not in objs:
                objs.append(obj)
            #if keep_lod_meshes:
            import_name = str(getattr(mesh, "import_name", "") or "").strip()
            obj.name = import_name or Path(mesh.meshName).stem
            obj['repo_path'] = scene_repo_key
            if embedded_cmesh_chunk_index is not None:
                obj['witcher_source_mesh_path'] = mesh.meshName
                obj['witcher_embedded_cmesh_chunk_index'] = embedded_cmesh_chunk_index
            for subobj in objs:
                if subobj == obj:
                    continue
                subobj.parent = obj
            # else:
            #     obj = objs[0]
            #     obj['repo_path'] = mesh.meshName
            #apply scale
            bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
            _record_duplicate_root(obj, subtree=objs)
            if defer_mats:
                queue_deferred_mesh_materials(resolved_cr2w_path, embedded_cmesh_chunk_index, objs, repo_path=mesh_name)
            finalize_seconds = time.perf_counter() - finalize_started
        except Exception:
            #usually tried to do something with materials and failed
            log.exception("Problem finalizing imported mesh %s", mesh.meshName)
            return
    else:
        reused_existing = True
    tree_objects = (reuse_clones or None) if reused_existing else objs
    if parent_transform:
        try:
            root_local_matrix = obj.matrix_local.copy()
        except Exception:
            root_local_matrix = None
        child_local_matrices = []
        try:
            children_iter = (
                [o for o in tree_objects if o is not obj and o.parent == obj]
                if tree_objects is not None
                else list(getattr(obj, "children", []) or [])
            )
            for child in children_iter:
                child_local_matrices.append((child, child.matrix_local.copy()))
        except Exception:
            child_local_matrices = []
        obj.parent = parent_transform
        if root_local_matrix is not None:
            _set_object_local_matrix_direct(obj, root_local_matrix)
        for child, local_matrix in child_local_matrices:
            _set_object_local_matrix_direct(child, local_matrix)

    transform_started = time.perf_counter()
    if mesh.transform:
        obj.rotation_euler = (0,0,0)
        #THIS WORKS?
        x, y, z = (
            radians(_transform_real(mesh.transform, "Yaw", 0.0)),
            radians(_transform_real(mesh.transform, "Pitch", 0.0)),
            radians(_transform_real(mesh.transform, "Roll", 0.0)),
        )
        orders =  ['XYZ', 'XZY', 'YXZ', 'YZX', 'ZXY', 'ZYX']
        mat = Euler((x, y, z), orders[2]).to_matrix().to_4x4()

        rotate_180 = False
        if rotate_180:
            mat[0][0], mat[0][1], mat[0][2] = -mat[0][0], -mat[0][1], mat[0][2]
            mat[1][0], mat[1][1], mat[1][2] = -mat[1][0], -mat[1][1], mat[1][2]
            mat[2][0], mat[2][1], mat[2][2] = -mat[2][0], -mat[2][1], mat[2][2]
        else:
            mat[0][0], mat[0][1], mat[0][2] = mat[0][0], mat[0][1], mat[0][2]
            mat[1][0], mat[1][1], mat[1][2] = mat[1][0], mat[1][1], mat[1][2]
            mat[2][0], mat[2][1], mat[2][2] = mat[2][0], mat[2][1], mat[2][2]

        obj.matrix_world @= mat
        # obj.rotation_euler[0] = mesh.transform.Pitch
        # obj.rotation_euler[1] = mesh.transform.Yaw
        # obj.rotation_euler[2] = mesh.transform.Roll
        obj.location[0] = _transform_real(mesh.transform, "X", 0.0)
        obj.location[1] = _transform_real(mesh.transform, "Y", 0.0)
        obj.location[2] = _transform_real(mesh.transform, "Z", 0.0)

        #foliage transforms don't have scale
        if isinstance(mesh.transform, dict) or hasattr(mesh.transform, "Scale_x"):
            obj.scale[0] = _transform_real(mesh.transform, "Scale_x", 1.0)
            obj.scale[1] = _transform_real(mesh.transform, "Scale_y", 1.0)
            obj.scale[2] = _transform_real(mesh.transform, "Scale_z", 1.0)
        # else:
        #     obj.scale[0] =0.01
        #     obj.scale[1] =0.01
        #     obj.scale[2] =0.01
    if mesh.matrix:
        try:
            #obj = bpy.context.selected_objects[:][0]
            #MATRIX PART
            log.debug(obj.name)
            mat = Matrix()

            rotate_180 = False
            if rotate_180:
                mat[0][0], mat[0][1], mat[0][2] = -mesh.matrix[0][0], -mesh.matrix[1][0], mesh.matrix[2][0]
                mat[1][0], mat[1][1], mat[1][2] = -mesh.matrix[0][1], -mesh.matrix[1][1], mesh.matrix[2][1]
                mat[2][0], mat[2][1], mat[2][2] = -mesh.matrix[0][2], -mesh.matrix[1][2], mesh.matrix[2][2]
            else:
                mat[0][0], mat[0][1], mat[0][2] = mesh.matrix[0][0], mesh.matrix[1][0], mesh.matrix[2][0]
                mat[1][0], mat[1][1], mat[1][2] = mesh.matrix[0][1], mesh.matrix[1][1], mesh.matrix[2][1]
                mat[2][0], mat[2][1], mat[2][2] = mesh.matrix[0][2], mesh.matrix[1][2], mesh.matrix[2][2]
            #log.info(mat)
            obj.matrix_world = obj.matrix_world @ mat
        except Exception:
            error_message = "ERROR MESH IMPORTER: Can't import: " + mesh.fbxPath()
            log.info(error_message)
            errors.append(error_message)
    if mesh.translation:
        translation = _extract_vector_position(mesh.translation)
        if translation is not None:
            obj.location[0] = translation[0]
            obj.location[1] = translation[1]
            obj.location[2] = translation[2]
    _tag_object_tree_for_layer_and_plan(
        obj,
        kwargs.get("_layer_import_owner"),
        kwargs.get("_layer_import_plan_item_id"),
        kwargs.get("_layer_import_plan_mode"),
        objects=tree_objects,
    )
    is_proxy_mesh = bool(getattr(mesh, "is_proxy_mesh", False)) or _path_indicates_proxy_mesh(getattr(mesh, "meshName", ""), "")
    if is_proxy_mesh:
        _tag_object_tree_as_proxy_mesh(obj, objects=tree_objects)
    elif (
        bool(obj.get("witcher_layer_proxy_mesh", False))
        or str(obj.get("witcher_layer_visibility_kind", "") or "").strip().lower() == "proxy_mesh"
    ):
        _clear_object_tree_proxy_mesh_tags(obj, objects=tree_objects)
    sector_flags = getattr(mesh, "sector_flags", None)
    if sector_flags is not None:
        try:
            _tag_object_tree_engine_visibility(
                obj,
                _sector_mesh_visible_from_flags(sector_flags, default=True),
                kwargs,
                sector_flags=sector_flags,
                objects=tree_objects,
            )
        except Exception:
            pass
    elif hasattr(mesh, "engine_visible"):
        try:
            _tag_object_tree_engine_visibility(
                obj,
                bool(getattr(mesh, "engine_visible")),
                kwargs,
                drawable_flags=getattr(mesh, "drawable_flags", None) if hasattr(mesh, "drawable_flags") else None,
                objects=tree_objects,
            )
        except Exception:
            pass
    transform_seconds = time.perf_counter() - transform_started
    total_seconds = time.perf_counter() - mesh_started
    _record_layer_mesh_profile(
        kwargs,
        mesh,
        backend,
        reused_existing,
        total_seconds,
        import_seconds,
        finalize_seconds,
        transform_seconds,
    )
    if total_seconds >= _MESH_IMPORT_WARN_THRESHOLD:
        _log_mesh_import_timing_warning(
            "single mesh %s total %.3fs (backend %s, import %.3fs, finalize %.3fs, transform %.3fs, reused %s, kind %s)",
            mesh.meshName,
            total_seconds,
            backend,
            import_seconds,
            finalize_seconds,
            transform_seconds,
            "yes" if reused_existing else "no",
            getattr(mesh, "type", ""),
        )
    return obj

MeshComponent_Type_List = ['CStaticMeshComponent',
                            'CMeshComponent',
                            'CDestructionComponent',
                            'CRigidMeshComponent',
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
                            "CWindowComponent"]

def getDataBufferMesh(
    entity,
    *,
    mesh_fbx_uncook_path=None,
    mesh_uncook_path=None,
    component_types=None,
):
    mesh_list = []
    cloth_list = []
    selected_types = set(component_types) if component_types is not None else None
    if hasattr(entity, "streamingDataBuffer") and entity.streamingDataBuffer:
        for chunk in entity.streamingDataBuffer.CHUNKS.CHUNKS:
            if is_entity_chunk(chunk):
                log.info("Found an entity in data buffer??")
            if (
                chunk.name in MeshComponent_Type_List
                and (selected_types is None or chunk.name in selected_types)
            ):
                try:
                    mesh = _new_mesh_path(
                        fbx_uncook_path=mesh_fbx_uncook_path,
                        uncook_path=mesh_uncook_path,
                        cr2w_version=getattr(chunk, "get_CR2W_version", lambda: 999)(),
                    ).static_from_chunk(chunk)
                except Exception as exc:
                    raise ValueError(f"Unable to resolve streamingDataBuffer mesh {chunk.name}: {exc}") from exc
                if not _mesh_repo_path(mesh):
                    raise ValueError(f"streamingDataBuffer mesh {chunk.name} resolved to an empty mesh path")
                drawable_flags = _component_drawable_flags(chunk)
                mesh.drawable_flags = drawable_flags
                mesh.engine_visible = _drawable_flags_visible_from_value(drawable_flags, default=True)
                mesh.component_type = chunk.name
                mesh.component_name = _component_prop_string(chunk, "name")
                mesh.component_action_name = _component_action_name(chunk)
                mesh_list.append(mesh)
            
            if (
                chunk.name in {"CClothComponent", "CDestructionSystemComponent"}
                and (selected_types is None or chunk.name in selected_types)
            ):
                cloth_list.append(chunk)

    return (mesh_list, cloth_list)

from .. import get_witcher2_game_path

def import_single_component(component, parent_obj, keep_lod_meshes = False, **kwargs):
    if component.name in {
        "CMeshComponent",
        "CStaticMeshComponent",
        "CDestructionComponent",
    }:
        try:
            component_type = component.name
            component_name = _component_prop_string(component, "name")
            action_name = _component_action_name(component)
            mesh = _new_mesh_path(
                fbx_uncook_path=get_fbx_uncook_path(bpy.context),
                uncook_path=kwargs.get("_mesh_uncook_path") or kwargs.get("mesh_uncook_path"),
                cr2w_version=getattr(component, "get_CR2W_version", lambda: 999)(),
            ).static_from_chunk(component)
            mesh_path = _mesh_repo_path(mesh)
            if not mesh_path:
                raise ValueError(f"{component.name} resolved to an empty mesh path")
            is_proxy_mesh = _path_indicates_proxy_mesh(mesh_path, "")
            if is_proxy_mesh and _proxy_mesh_filter_active(kwargs):
                if not bool(kwargs.get("do_import_ProxyMesh", False)):
                    return None
            elif not bool(kwargs.get("do_import_Mesh", True)):
                return None
            # if component.get_CR2W_version() <= 115:
            #     mesh.uncook_path = get_witcher2_game_path(bpy.context) + '\\data'
            mesh_label = str(getattr(mesh, "name", "") or "").strip()
            if not mesh_label or mesh_label == "Mesh Item":
                mesh_label = Path(mesh_path).stem or component.name
            drawable_flags = _component_drawable_flags(component)
            mesh.drawable_flags = drawable_flags
            engine_visible = _drawable_flags_visible_from_value(drawable_flags, default=True)
            mesh.engine_visible = engine_visible
            mesh.component_type = component_type
            mesh.component_name = component_name
            mesh.component_action_name = action_name
            mesh.import_name = mesh_label
            return _import_component_mesh_from_mesh(
                mesh,
                [],
                parent_obj,
                component_type=component_type,
                component_name=component_name,
                component_transform=getattr(mesh, "transform", None),
                drawable_flags=drawable_flags,
                engine_visible=engine_visible,
                action_name=action_name,
                keep_lod_meshes=keep_lod_meshes or (is_proxy_mesh and bool(kwargs.get("keep_proxy_meshes", True))),
                version=component.get_CR2W_version(),
                kwargs=kwargs,
            )
        except MeshReferenceMissing as exc:
            log.debug("Importing meshless %s as empty: %s", component.name, exc)
            return _import_meshless_component_empty(component, parent_obj, **kwargs)
        except Exception as e:
            log.exception("import_single_component mesh fail: %s", e) #w2 has embedded here??
            raise
    elif component.name in {"CPointLightComponent", "CSpotLightComponent"}:
        option_name = (
            "do_import_SpotLight"
            if component.name == "CSpotLightComponent"
            else "do_import_PointLight"
        )
        if not bool(kwargs.get(option_name, True)):
            return None
        bpy.ops.object.light_add(type='POINT', radius=1, align='WORLD', location=(0, 0, 0), scale=(1, 1, 1))
        light_obj = bpy.context.selected_objects[:][0]
        light_obj.parent = parent_obj
        name = _component_prop_string(component, "name")
        if name:
            light_obj.name = name
            light_obj.data.name = name
        configure_entity_light(
            light_obj,
            _component_light_properties(component),
            component.name,
            scene=bpy.context.scene,
        )
        transform = component.GetVariableByName('transform')
        if transform:
            set_blender_object_transform(light_obj, transform.EngineTransform)
        if component.name == "CSpotLightComponent":
            orient_red_spot(light_obj)
        return light_obj

