"""Blender context capture and rollback helpers for cloth imports."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

import bpy


log = logging.getLogger(__name__)

__all__ = (
    "ClothImportState",
    "cleanup_failed_cloth_import",
    "resolve_collection_ref",
    "restore_active_layer_collection",
    "snapshot_blender_import_state",
)


def _bpy_data_block_identity(data_block):
    if data_block is None:
        return None
    try:
        return int(data_block.as_pointer())
    except Exception:
        return id(data_block)


def resolve_collection_ref(collection_ref, context=None):
    """Resolve a collection, layer collection, or collection name."""
    if collection_ref is None:
        return None
    if hasattr(collection_ref, "collection") and getattr(collection_ref, "collection", None) is not None:
        return collection_ref.collection
    if hasattr(collection_ref, "objects") and hasattr(collection_ref, "children"):
        return collection_ref
    if collection_ref == "Scene Collection":
        ctx = context or bpy.context
        scene = getattr(ctx, "scene", None)
        return getattr(scene, "collection", None)
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


def restore_active_layer_collection(context, target_collection) -> bool:
    """Make the layer collection for ``target_collection`` active again."""
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


def snapshot_blender_import_state() -> dict[str, set[int]]:
    """Capture the identities of Blender data blocks owned before an import."""
    return {
        "objects": {_bpy_data_block_identity(obj) for obj in bpy.data.objects},
        "collections": {_bpy_data_block_identity(coll) for coll in bpy.data.collections},
        "meshes": {_bpy_data_block_identity(mesh) for mesh in bpy.data.meshes},
        "armatures": {_bpy_data_block_identity(arm) for arm in bpy.data.armatures},
        "materials": {_bpy_data_block_identity(mat) for mat in bpy.data.materials},
        "node_groups": {_bpy_data_block_identity(group) for group in bpy.data.node_groups},
    }


def _object_parent_depth(obj) -> int:
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


def _collection_parent_depth(collection, _memo=None) -> int:
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


def _new_data_blocks(snapshot_state, snapshot_key, data_blocks):
    known = snapshot_state.get(snapshot_key, set()) if snapshot_state else set()
    return [
        data_block
        for data_block in data_blocks
        if _bpy_data_block_identity(data_block) not in known
    ]


def cleanup_failed_cloth_import(snapshot_state) -> None:
    """Remove data blocks created after ``snapshot_state`` was captured."""
    if not snapshot_state:
        return

    new_objects = _new_data_blocks(snapshot_state, "objects", bpy.data.objects)
    new_objects.sort(key=_object_parent_depth, reverse=True)
    for obj in new_objects:
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
        except Exception as exc:
            log.debug(
                "Failed removing partial cloth import object %s: %s",
                getattr(obj, "name", "<unknown>"),
                exc,
            )

    depth_cache = {}
    new_collections = _new_data_blocks(snapshot_state, "collections", bpy.data.collections)
    new_collections.sort(
        key=lambda coll: _collection_parent_depth(coll, depth_cache),
        reverse=True,
    )
    for coll in new_collections:
        try:
            for parent in list(_iter_collection_parents(coll)):
                try:
                    parent.children.unlink(coll)
                except Exception:
                    pass
            bpy.data.collections.remove(coll)
        except Exception as exc:
            log.debug(
                "Failed removing partial cloth import collection %s: %s",
                getattr(coll, "name", "<unknown>"),
                exc,
            )

    orphan_specs = (
        (bpy.data.node_groups, "node_groups"),
        (bpy.data.materials, "materials"),
        (bpy.data.meshes, "meshes"),
        (bpy.data.armatures, "armatures"),
    )
    for data_blocks, snapshot_key in orphan_specs:
        for data_block in list(data_blocks):
            if _bpy_data_block_identity(data_block) in snapshot_state[snapshot_key]:
                continue
            try:
                if getattr(data_block, "users", 0) == 0:
                    data_blocks.remove(data_block)
            except Exception as exc:
                log.debug(
                    "Failed removing partial cloth import data block %s: %s",
                    getattr(data_block, "name", "<unknown>"),
                    exc,
                )


@dataclass
class ClothImportState:
    """Saved Blender UI and data-block state for one explicit cloth import."""

    context: Any
    selected_objects: tuple[Any, ...]
    active_object: Any
    active_collection: Any
    data_snapshot: dict[str, set[int]]

    @classmethod
    def capture(cls, context=None) -> "ClothImportState":
        ctx = context or bpy.context
        view_layer = getattr(ctx, "view_layer", None)
        active_layer_collection = getattr(view_layer, "active_layer_collection", None)
        try:
            selected_objects = tuple(ctx.selected_objects)
        except Exception:
            selected_objects = tuple(bpy.context.selected_objects[:])
        return cls(
            context=ctx,
            selected_objects=selected_objects,
            active_object=getattr(getattr(view_layer, "objects", None), "active", None),
            active_collection=getattr(active_layer_collection, "collection", None),
            data_snapshot=snapshot_blender_import_state(),
        )

    def new_objects(self) -> list[Any]:
        """Return objects created since this state was captured."""
        return _new_data_blocks(self.data_snapshot, "objects", bpy.data.objects)

    def new_collections(self) -> list[Any]:
        """Return collections created since this state was captured."""
        return _new_data_blocks(self.data_snapshot, "collections", bpy.data.collections)

    def restore_context(self, selection=True) -> None:
        ctx = self.context or bpy.context
        view_layer = getattr(ctx, "view_layer", None)
        if selection:
            try:
                view_layer.objects.active = None
            except Exception:
                pass
            try:
                bpy.ops.object.select_all(action="DESELECT")
            except Exception:
                pass
            try:
                view_layer.objects.active = self.active_object
            except Exception:
                pass
            for obj in self.selected_objects:
                try:
                    obj.select_set(True)
                except Exception:
                    pass
        try:
            restore_active_layer_collection(ctx, self.active_collection)
        except Exception:
            pass

    def rollback(self) -> None:
        """Remove Blender data blocks created since this state was captured."""
        cleanup_failed_cloth_import(self.data_snapshot)
