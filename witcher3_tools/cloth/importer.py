"""Import orchestration for REDcloth/APX assets."""

import inspect
import json
import logging
import os
import time
from pathlib import Path

import addon_utils
import bmesh
import bpy
from bpy.types import Object

from .. import get_DO_WEAR_CLOTH, get_do_fix_tail
from ..importers.import_rig import rotate_and_connect_bones
from .apx import sanitize_apx_for_import
from .geometry_nodes import (
    apply_redcloth_runtime_defaults,
    create_collision_proxy_object,
    fix_connection_objects_transform_space,
    patch_clothsimulation_to_object_proxies,
)
from .import_state import ClothImportState, resolve_collection_ref
from .materials import apply_redcloth_materials_to_meshes


log = logging.getLogger(__name__)
_REDCLOTH_PROFILE_ENABLED = True
_REDCLOTH_PROFILE_WARN_THRESHOLD = 0.10

__all__ = ["import_cloth"]


def _log_redcloth_profile_warning(message, *args):
    if not _REDCLOTH_PROFILE_ENABLED:
        return
    log.info("[redcloth-profile] " + str(message), *args)


def _move_objects_between_collections(old_collection_name, new_collection_name):
    # Get the master collection (Scene Collection)
    master_collection = bpy.context.scene.collection

    old_collection = resolve_collection_ref(old_collection_name)
    if old_collection is None:
        log.warning("Old collection '%s' not found.", old_collection_name)
        return

    new_collection = resolve_collection_ref(new_collection_name)
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


def _create_empty(prefix=None, name="", parent=None):
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
        obj.hide_render = True
        obj.display_type = 'SOLID'
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


def _find_imported_collection(collections, base_name: str, owner_collection=None):
    """Find this import's Blender-suffixed APX collection by its base name."""
    candidates = [
        collection
        for collection in (collections or [])
        if collection.name == base_name or collection.name.startswith(base_name + ".")
    ]
    if owner_collection is not None:
        for collection in candidates:
            try:
                if collection.name in owner_collection.children.keys():
                    return collection
            except Exception:
                pass
    candidates.sort(key=lambda collection: collection.name)
    return candidates[0] if candidates else None


def _color_to_pin_weights(obj, src_vcol, dst_vgroup_idx):
    mesh = obj.data
    group = obj.vertex_groups[dst_vgroup_idx]
    for index in range(len(mesh.vertices)):
        color_value = src_vcol.data[index]
        color = color_value.color if hasattr(color_value, "color") else color_value.vector
        pin_weight = 1.0 - color[1]
        pin_weight = pin_weight if pin_weight > 0.99 else pin_weight / 2.5
        group.add([index], pin_weight, "REPLACE")

    mesh.update()


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


def import_cloth(context, filepath, use_mat, rotate_180, rm_ph_me, mat_filename=""):
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

    context = context or bpy.context
    import_state = ClothImportState.capture(context)
    save_collection = import_state.active_collection

    # Global addon preference is the single source of truth.
    do_wear_cloth = bool(get_DO_WEAR_CLOTH(context))

    if not filepath or not os.path.isfile(filepath):
        log.warning("Skipping redcloth import, APX/APB file not found: %s", filepath)
        return None

    sanitize_started = time.perf_counter()
    filepath = sanitize_apx_for_import(filepath)
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
        imported_objects = import_state.new_objects()
        imported_collections = import_state.new_collections()
        # objs = bpy.context.objects[:]
        # for obj in objs:
        #     print (obj.name)

        bpy.context.view_layer.objects.active = None
        bpy.ops.object.select_all(action='DESELECT')
        active_layer_collection = getattr(bpy.context.view_layer, "active_layer_collection", None)
        active_coll = getattr(active_layer_collection, "collection", None) or save_collection
        if active_coll is None:
            raise RuntimeError(f"No active collection available after APX import for {filepath}")
        arma = None
        armature_scan_started = time.perf_counter()
        arma_objs = [
            obj
            for obj in imported_objects
            if obj.type == "ARMATURE" and "Armature" in obj.name
        ]
        arma_objs.sort(key=lambda x: x.name, reverse=True)
        armature_scan_seconds = time.perf_counter() - armature_scan_started
        if not arma_objs:
            raise RuntimeError(f"No APX armature found after import for {filepath}")
        arma = arma_objs[0]
        filename = Path(filepath).stem

        do_fix_tail = get_do_fix_tail(bpy.context)
        if do_fix_tail:
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

        if do_wear_cloth:
            collision_started = time.perf_counter()
            cloth_group = _create_empty(filename, "_grp")
            collision_transform = _create_empty(filename, "Collision Spheres", cloth_group)
            connections_transform = _create_empty(filename, "Collision Connections", cloth_group)
            proxy_transform = _create_empty(filename, "Collision Proxies", cloth_group)
            arma.parent = cloth_group

            arma.name = filename
            arma.data.name = filename+"_ARM"
            arma.select_set(True)
            bpy.context.view_layer.objects.active = arma

            spheres_coll = _find_imported_collection(
                imported_collections,
                "Collision Spheres",
                active_coll,
            )
            connect_coll = _find_imported_collection(
                imported_collections,
                "Collision Connections",
                active_coll,
            )
            capsules_coll = _find_imported_collection(
                imported_collections,
                "Collision Capsules",
                active_coll,
            )

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

            fix_connection_objects_transform_space(all_connect_coll)
            _parent_and_namespace_collision_objects(filename, collision_transform, all_spheres_coll, keep_transform=False)
            _parent_and_namespace_collision_objects(filename, connections_transform, all_connect_coll, keep_transform=False)
            if all_capsules_coll:
                capsules_transform = _create_empty(filename, "Collision Capsules", cloth_group)
                _parent_and_namespace_collision_objects(filename, capsules_transform, all_capsules_coll, keep_transform=False)

            collision_proxy_objects["spheres"] = create_collision_proxy_object(
                _namespaced_name(filename, "Collision Spheres Proxy"),
                proxy_transform,
                active_coll,
                all_spheres_coll,
            )
            collision_proxy_objects["connections"] = create_collision_proxy_object(
                _namespaced_name(filename, "Collision Connections Proxy"),
                proxy_transform,
                active_coll,
                all_connect_coll,
            )
            collision_proxy_objects["capsules"] = create_collision_proxy_object(
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
        gmesh_objects = [
            obj
            for obj in imported_objects
            if obj.type == "MESH" and obj.name.startswith("GMesh_lod")
        ]
        gmesh_objects.sort(key=lambda obj: obj.name)
        gmesh_count = len(gmesh_objects)
        mesh_scan_seconds = time.perf_counter() - mesh_scan_started
        if not gmesh_objects:
            raise RuntimeError(f"No GMesh_lod mesh found after APX import for {filepath}")
        gmesh = gmesh_objects[0]
        if do_wear_cloth:
            gmesh.name = filename+":"+gmesh.name
        mesh_name_payload = json.dumps([gmesh.name])
        try:
            arma["witcher_redcloth_mesh_name"] = gmesh.name
            arma["witcher_redcloth_mesh_names"] = mesh_name_payload
            if do_wear_cloth and 'cloth_group' in locals():
                cloth_group["witcher_redcloth_mesh_name"] = gmesh.name
                cloth_group["witcher_redcloth_mesh_names"] = mesh_name_payload
        except Exception:
            pass

        for o in reversed(gmesh_objects):
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
        apply_redcloth_runtime_defaults(gmesh, context)
        runtime_defaults_seconds = time.perf_counter() - runtime_defaults_started

        if do_wear_cloth:
            patch_started = time.perf_counter()
            patched_collision_mode = patch_clothsimulation_to_object_proxies(
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

                _color_to_pin_weights(gmesh, vcol, vgroup.index)
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
            _move_objects_between_collections(active_coll, save_collection)
            move_seconds = time.perf_counter() - move_started
            if move_seconds >= _REDCLOTH_PROFILE_WARN_THRESHOLD:
                _log_redcloth_profile_warning(
                    "move collections %s %.3fs",
                    filename,
                    move_seconds,
                )

        restore_started = time.perf_counter()
        import_state.restore_context()
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
                "yes" if do_wear_cloth else "no",
            )

        if do_wear_cloth:
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
                "yes" if do_wear_cloth else "no",
                e,
            )
        log.warning("Redcloth import failed for %s: %s", os.path.basename(filepath), e)
        log.debug("Redcloth import traceback for %s", filepath, exc_info=True)
        try:
            import_state.rollback()
        except Exception as cleanup_exc:
            log.debug("Failed cleaning up partial cloth import for %s: %s", filepath, cleanup_exc)
        restore_started = time.perf_counter()
        import_state.restore_context()
        restore_seconds = time.perf_counter() - restore_started
        return None
