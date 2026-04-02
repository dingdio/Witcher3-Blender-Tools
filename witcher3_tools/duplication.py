"""Centralized helpers for Blender object duplication.

1. Character-linked duplication
   This duplicates a full character hierarchy, keeps most mesh data linked, and retargets internal object references so the duplicate points at itself.

2. Morph-bake duplication
   This duplicates temporary evaluation objects for face baking. Those copies intentionally own their mesh/armature data and clear animation data.
"""

import logging
import uuid

import bpy

log = logging.getLogger(__name__)

__all__ = [
    "duplicate_character_hierarchy",
    "duplicate_object_for_morph_bake",
    "remap_duplicated_object_links",
]


def duplicate_object_for_morph_bake(obj):
    """Create a temporary duplicate used by the morph-bake pipeline."""
    duplicate = obj.copy()
    duplicate.name = f"{obj.name}__W3MorphBake"
    copied_data = None
    data = getattr(obj, "data", None)
    if data is not None and getattr(obj, "type", None) in {"MESH", "ARMATURE"} and hasattr(data, "copy"):
        copied_data = data.copy()
        duplicate.data = copied_data
    try:
        if duplicate.animation_data is not None:
            duplicate.animation_data_clear()
    except Exception:
        pass
    return duplicate, copied_data


def _remap_object_reference(owner, attr_name, object_map):
    try:
        target = getattr(owner, attr_name, None)
    except Exception:
        return
    if target is None:
        return
    mapped_target = object_map.get(getattr(target, "name", ""))
    if mapped_target is None:
        return
    try:
        setattr(owner, attr_name, mapped_target)
    except Exception:
        pass


def _remap_constraint_targets(constraints, object_map):
    for constraint in constraints or []:
        _remap_object_reference(constraint, "target", object_map)
        _remap_object_reference(constraint, "space_object", object_map)
        for target_slot in getattr(constraint, "targets", []) or []:
            _remap_object_reference(target_slot, "target", object_map)


def _remap_modifier_targets(modifiers, object_map):
    for modifier in modifiers or []:
        for attr_name in (
            "object",
            "mirror_object",
            "offset_object",
            "start_cap",
            "end_cap",
            "target",
            "origin",
            "object_from",
            "object_to",
        ):
            _remap_object_reference(modifier, attr_name, object_map)
        for projector in getattr(modifier, "projectors", []) or []:
            _remap_object_reference(projector, "object", object_map)
        for target_slot in getattr(modifier, "targets", []) or []:
            _remap_object_reference(target_slot, "target", object_map)


def remap_duplicated_object_links(
    original_obj,
    duplicate_obj,
    object_map,
    *,
    remap_parent=True,
    remap_constraints=True,
    remap_modifiers=True,
    remap_pose_bone_data=True,
):
    """Retarget duplicated object links from source objects to duplicate objects."""
    if original_obj is None or duplicate_obj is None:
        return

    if remap_parent:
        original_parent = getattr(original_obj, "parent", None)
        duplicate_parent = object_map.get(getattr(original_parent, "name", "")) if original_parent is not None else None
        try:
            duplicate_obj.parent = duplicate_parent
            duplicate_obj.parent_type = getattr(original_obj, "parent_type", "OBJECT")
            duplicate_obj.parent_bone = getattr(original_obj, "parent_bone", "")
            duplicate_obj.matrix_parent_inverse = original_obj.matrix_parent_inverse.copy()
        except Exception:
            pass

    if remap_constraints:
        _remap_constraint_targets(getattr(duplicate_obj, "constraints", None), object_map)

    if remap_modifiers:
        _remap_modifier_targets(getattr(duplicate_obj, "modifiers", None), object_map)

    # Pose-bone links are easy to miss when duplicating armatures. Keeping this
    # here means both character duplication and morph baking use the same rules.
    if remap_pose_bone_data and getattr(duplicate_obj, "type", None) == "ARMATURE":
        original_pose_bones = getattr(getattr(original_obj, "pose", None), "bones", None)
        for pose_bone in getattr(getattr(duplicate_obj, "pose", None), "bones", []) or []:
            original_pose_bone = original_pose_bones.get(pose_bone.name) if original_pose_bones is not None else None
            if original_pose_bone is not None:
                _remap_object_reference(pose_bone, "custom_shape", object_map)
            _remap_constraint_targets(getattr(pose_bone, "constraints", None), object_map)


def _duplicate_id_key(id_block):
    if id_block is None:
        return None
    try:
        return (type(id_block).__name__, int(id_block.as_pointer()))
    except Exception:
        return (type(id_block).__name__, id(id_block))


def _iter_character_duplicate_objects(root_armature):
    ordered = []
    seen = set()
    for obj in [root_armature] + list(getattr(root_armature, "children_recursive", []) or []):
        if obj is None:
            continue
        try:
            key = obj.as_pointer()
        except Exception:
            key = id(obj)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(obj)
    return ordered


def _link_duplicate_to_source_collections(context, source_obj, duplicate_obj):
    linked = False
    for collection in getattr(source_obj, "users_collection", []) or []:
        try:
            collection.objects.link(duplicate_obj)
            linked = True
        except Exception:
            continue
    if linked:
        return
    target_collection = getattr(context, "collection", None)
    if target_collection is None:
        target_collection = getattr(getattr(context, "scene", None), "collection", None)
    if target_collection is not None:
        target_collection.objects.link(duplicate_obj)


def _mesh_duplicate_requires_unique_data(obj):
    if obj is None or getattr(obj, "type", "") != "MESH":
        return False
    data = getattr(obj, "data", None)
    if data is None:
        return False
    if getattr(data, "shape_keys", None) is not None:
        return True
    animation_data = getattr(data, "animation_data", None)
    drivers = getattr(animation_data, "drivers", None) if animation_data is not None else None
    return bool(drivers)


def _duplicate_character_object(source_obj):
    duplicate_obj = source_obj.copy()
    copied_data = None
    data = getattr(source_obj, "data", None)
    should_copy_data = (
        getattr(source_obj, "type", "") == "ARMATURE"
        or _mesh_duplicate_requires_unique_data(source_obj)
    )
    if should_copy_data and data is not None and hasattr(data, "copy"):
        copied_data = data.copy()
        duplicate_obj.data = copied_data
    return duplicate_obj, copied_data


def _capture_duplicate_visibility_state(obj):
    state = {
        "hide_viewport": bool(getattr(obj, "hide_viewport", False)),
        "hide_render": bool(getattr(obj, "hide_render", False)),
        "hide_select": bool(getattr(obj, "hide_select", False)),
        "hidden": False,
    }
    try:
        state["hidden"] = bool(obj.hide_get())
    except TypeError:
        try:
            state["hidden"] = bool(obj.hide_get(bpy.context.view_layer))
        except Exception:
            pass
    except Exception:
        pass
    return state


def _apply_duplicate_visibility_state(obj, state):
    if obj is None or not state:
        return
    try:
        obj.hide_viewport = bool(state.get("hide_viewport", False))
    except Exception:
        pass
    try:
        obj.hide_render = bool(state.get("hide_render", False))
    except Exception:
        pass
    try:
        obj.hide_select = bool(state.get("hide_select", False))
    except Exception:
        pass
    try:
        obj.hide_set(bool(state.get("hidden", False)))
    except Exception:
        pass


def _remap_duplicate_id_attr(owner, attr_name, id_map):
    try:
        current = getattr(owner, attr_name, None)
    except Exception:
        return
    mapped = id_map.get(_duplicate_id_key(current))
    if mapped is None:
        return
    try:
        setattr(owner, attr_name, mapped)
    except Exception:
        pass


def _remap_duplicate_pointer_properties(owner, id_map, skip_attrs=None, object_only=False):
    if owner is None:
        return
    skip_attrs = set(skip_attrs or ())
    properties = getattr(getattr(owner, "bl_rna", None), "properties", None)
    if not properties:
        return
    for prop in properties:
        attr_name = getattr(prop, "identifier", "")
        if not attr_name or attr_name == "rna_type" or attr_name in skip_attrs:
            continue
        if getattr(prop, "is_readonly", False):
            continue
        if getattr(prop, "type", "") != "POINTER":
            continue
        try:
            current = getattr(owner, attr_name, None)
        except Exception:
            continue
        if current is None:
            continue
        if object_only and not isinstance(current, bpy.types.Object):
            continue
        mapped = id_map.get(_duplicate_id_key(current))
        if mapped is None:
            continue
        try:
            setattr(owner, attr_name, mapped)
        except Exception:
            pass


def _remap_duplicate_object_parent(source_obj, duplicate_obj, simple_object_map):
    if source_obj is None or duplicate_obj is None:
        return
    source_parent = getattr(source_obj, "parent", None)
    if source_parent is None:
        try:
            duplicate_obj.parent = None
        except Exception:
            pass
        return
    mapped_parent = simple_object_map.get(getattr(source_parent, "name", ""))
    if mapped_parent is None:
        return
    try:
        duplicate_obj.parent = mapped_parent
    except Exception:
        return
    for attr_name in ("parent_type", "parent_bone"):
        try:
            setattr(duplicate_obj, attr_name, getattr(source_obj, attr_name))
        except Exception:
            pass
    try:
        duplicate_obj.matrix_parent_inverse = source_obj.matrix_parent_inverse.copy()
    except Exception:
        pass


def _remap_duplicate_object_constraints(duplicate_obj, id_map):
    for constraint in getattr(duplicate_obj, "constraints", []) or []:
        _remap_duplicate_pointer_properties(constraint, id_map, object_only=True)


def _remap_duplicate_object_modifiers(duplicate_obj, id_map):
    for modifier in getattr(duplicate_obj, "modifiers", []) or []:
        _remap_duplicate_pointer_properties(
            modifier,
            id_map,
            skip_attrs={"node_group"},
            object_only=True,
        )


def _duplicate_modifier_node_group(node_group, node_group_map):
    if node_group is None:
        return None
    group_key = _duplicate_id_key(node_group)
    cached = node_group_map.get(group_key)
    if cached is not None:
        return cached

    copied_group = node_group.copy()
    node_group_map[group_key] = copied_group

    for node in getattr(copied_group, "nodes", []) or []:
        if (
            getattr(node, "bl_idname", "") != "GeometryNodeGroup"
            and getattr(node, "type", "") != "GROUP"
        ):
            continue
        sub_tree = getattr(node, "node_tree", None)
        if sub_tree is None:
            continue
        copied_sub_tree = _duplicate_modifier_node_group(sub_tree, node_group_map)
        if copied_sub_tree is not None:
            try:
                node.node_tree = copied_sub_tree
            except Exception:
                pass
    return copied_group


def _remap_duplicate_node_group_refs(node_group, id_map, visited=None):
    if node_group is None:
        return
    if visited is None:
        visited = set()
    group_key = _duplicate_id_key(node_group)
    if group_key in visited:
        return
    visited.add(group_key)

    for node in getattr(node_group, "nodes", []) or []:
        for attr_name in ("object", "target", "image", "material", "texture"):
            _remap_duplicate_id_attr(node, attr_name, id_map)

        for socket in getattr(node, "inputs", []) or []:
            try:
                socket_value = socket.default_value
            except Exception:
                continue
            if not hasattr(socket_value, "as_pointer"):
                continue
            mapped_value = id_map.get(_duplicate_id_key(socket_value))
            if mapped_value is None:
                continue
            try:
                socket.default_value = mapped_value
            except Exception:
                pass

        if (
            getattr(node, "bl_idname", "") == "GeometryNodeGroup"
            or getattr(node, "type", "") == "GROUP"
        ):
            _remap_duplicate_node_group_refs(getattr(node, "node_tree", None), id_map, visited=visited)


def _remap_duplicate_modifier_node_groups(duplicate_obj, id_map, node_group_map):
    for modifier in getattr(duplicate_obj, "modifiers", []) or []:
        modifier_keys = []
        if getattr(modifier, "type", "") == "NODES":
            try:
                modifier_keys = list(modifier.keys())
            except Exception:
                modifier_keys = []
        for key in modifier_keys:
            try:
                current_value = modifier[key]
            except Exception:
                continue
            mapped_value = id_map.get(_duplicate_id_key(current_value))
            if mapped_value is None:
                continue
            try:
                modifier[key] = mapped_value
            except Exception:
                pass

        node_group = getattr(modifier, "node_group", None)
        if node_group is None:
            continue
        copied_group = _duplicate_modifier_node_group(node_group, node_group_map)
        if copied_group is None:
            continue
        try:
            modifier.node_group = copied_group
        except Exception:
            continue
        _remap_duplicate_node_group_refs(copied_group, id_map)


def _remap_duplicate_driver_targets(animation_data, id_map):
    if animation_data is None:
        return
    drivers = getattr(animation_data, "drivers", None)
    if not drivers:
        return
    for fcurve in drivers:
        driver = getattr(fcurve, "driver", None)
        if driver is None:
            continue
        for var in getattr(driver, "variables", []) or []:
            for target in getattr(var, "targets", []) or []:
                target_id = getattr(target, "id", None)
                mapped_id = id_map.get(_duplicate_id_key(target_id))
                if mapped_id is None:
                    continue
                try:
                    target.id = mapped_id
                except Exception:
                    pass


def _remap_duplicate_object_drivers(duplicate_obj, id_map):
    _remap_duplicate_driver_targets(getattr(duplicate_obj, "animation_data", None), id_map)
    data = getattr(duplicate_obj, "data", None)
    if data is not None:
        _remap_duplicate_driver_targets(getattr(data, "animation_data", None), id_map)
        shape_keys = getattr(data, "shape_keys", None)
        if shape_keys is not None:
            _remap_duplicate_driver_targets(getattr(shape_keys, "animation_data", None), id_map)


def _retarget_duplicate_character_metadata(source_armature, duplicate_armature, object_map):
    source_arm_name = getattr(source_armature, "name_full", getattr(source_armature, "name", ""))
    duplicate_arm_name = getattr(duplicate_armature, "name_full", getattr(duplicate_armature, "name", ""))

    for source_obj, duplicate_obj in object_map.values():
        try:
            if duplicate_obj.get("witcher_owner_armature") == source_arm_name:
                duplicate_obj["witcher_owner_armature"] = duplicate_arm_name
        except Exception:
            pass

        mimic_face_name = str(source_obj.get("mimicFace", "") or "").strip()
        if mimic_face_name:
            duplicate_face = object_map.get(mimic_face_name, (None, None))[1]
            if duplicate_face is not None:
                try:
                    duplicate_obj["mimicFace"] = duplicate_face.name
                except Exception:
                    pass
        for prop_name in (
            "witcher_apx_spheres_proxy",
            "witcher_apx_connections_proxy",
            "witcher_apx_capsules_proxy",
        ):
            target_name = str(duplicate_obj.get(prop_name, "") or "").strip()
            mapped_target = object_map.get(target_name, (None, None))[1] if target_name else None
            if mapped_target is not None:
                try:
                    duplicate_obj[prop_name] = mapped_target.name
                except Exception:
                    pass


def _retarget_duplicate_character_guids(duplicate_armature, duplicate_records):
    equip_guid_map = {}
    template_guid_map = {}

    def _map_guid(old_guid, mapping):
        old_guid = str(old_guid or "").strip()
        if not old_guid:
            return ""
        if old_guid not in mapping:
            mapping[old_guid] = str(uuid.uuid4())
        return mapping[old_guid]

    for _source_obj, duplicate_obj, _copied_data, _visibility_state in duplicate_records:
        for prop_name, guid_map in (
            ("witcher_equip_guid", equip_guid_map),
            ("witcher_template_guid", template_guid_map),
        ):
            try:
                old_guid = duplicate_obj.get(prop_name, "")
            except Exception:
                old_guid = ""
            new_guid = _map_guid(old_guid, guid_map)
            if new_guid:
                try:
                    duplicate_obj[prop_name] = new_guid
                except Exception:
                    pass

        try:
            old_parent_guid = duplicate_obj.get("witcher_bound_parent_guid", "")
        except Exception:
            old_parent_guid = ""
        new_parent_guid = _map_guid(old_parent_guid, equip_guid_map)
        if new_parent_guid:
            try:
                duplicate_obj["witcher_bound_parent_guid"] = new_parent_guid
            except Exception:
                pass

    rig_settings = getattr(getattr(duplicate_armature, "data", None), "witcherui_RigSettings", None)
    if rig_settings is not None:
        for slot in getattr(rig_settings, "equipment_slots", []) or []:
            new_guid = _map_guid(getattr(slot, "equip_guid", ""), equip_guid_map)
            if new_guid:
                slot.equip_guid = new_guid
        for slot in getattr(rig_settings, "template_slots", []) or []:
            new_guid = _map_guid(getattr(slot, "template_guid", ""), template_guid_map)
            if new_guid:
                slot.template_guid = new_guid


def duplicate_character_hierarchy(context, source_armature):
    """Create a linked duplicate of a character hierarchy."""
    source_objects = _iter_character_duplicate_objects(source_armature)
    if not source_objects:
        return None

    object_map = {}
    id_map = {}
    node_group_map = {}
    duplicate_records = []

    # First pass: create and link every duplicate object so Blender gives each
    # copy a real identity before we start fixing references.
    for source_obj in source_objects:
        duplicate_obj, copied_data = _duplicate_character_object(source_obj)
        _link_duplicate_to_source_collections(context, source_obj, duplicate_obj)
        visibility_state = _capture_duplicate_visibility_state(source_obj)
        object_map[source_obj.name] = (source_obj, duplicate_obj)
        duplicate_records.append((source_obj, duplicate_obj, copied_data, visibility_state))
        id_map[_duplicate_id_key(source_obj)] = duplicate_obj
        if copied_data is not None:
            id_map[_duplicate_id_key(getattr(source_obj, "data", None))] = copied_data

    simple_object_map = {name: duplicate for name, (_source, duplicate) in object_map.items()}

    # Second pass: every duplicate exists now, so internal references can be
    # pointed back at the duplicate hierarchy instead of the source hierarchy.
    for source_obj, duplicate_obj, _copied_data, visibility_state in duplicate_records:
        _remap_duplicate_object_parent(source_obj, duplicate_obj, simple_object_map)
        _remap_duplicate_object_constraints(duplicate_obj, id_map)
        _remap_duplicate_object_modifiers(duplicate_obj, id_map)
        remap_duplicated_object_links(
            source_obj,
            duplicate_obj,
            simple_object_map,
            remap_parent=False,
            remap_constraints=False,
            remap_modifiers=False,
            remap_pose_bone_data=True,
        )
        _remap_duplicate_modifier_node_groups(duplicate_obj, id_map, node_group_map)
        _remap_duplicate_object_drivers(duplicate_obj, id_map)
        _apply_duplicate_visibility_state(duplicate_obj, visibility_state)

    duplicate_armature = simple_object_map.get(source_armature.name)
    if duplicate_armature is None:
        return None

    _retarget_duplicate_character_metadata(source_armature, duplicate_armature, object_map)
    _retarget_duplicate_character_guids(duplicate_armature, duplicate_records)

    try:
        duplicate_armature.matrix_world = source_armature.matrix_world.copy()
    except Exception:
        pass

    rig_settings = getattr(getattr(duplicate_armature, "data", None), "witcherui_RigSettings", None)
    if rig_settings is not None:
        try:
            rig_settings.model_armature_object = duplicate_armature
        except Exception:
            pass

    return duplicate_armature
