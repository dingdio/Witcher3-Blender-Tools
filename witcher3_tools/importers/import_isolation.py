"""Helpers for running import code inside a temporary isolated Blender context.

usage:
    base_context = context or bpy.context
    if import_isolation.needs_isolation_session(base_context):
        with import_isolation.isolated_import_session(...) as session:
            return _same_public_function(session.context, ...)
"""

import logging
from contextlib import contextmanager
from dataclasses import dataclass

import bpy

log = logging.getLogger(__name__)

__all__ = [
    "ENABLE_ISOLATED_IMPORTS",
    "ImportIsolationSession",
    "collect_related_hierarchy_objects",
    "import_isolation_enabled",
    "is_isolated_import_context",
    "isolated_import_session",
    "needs_isolation_session",
    "set_import_isolation_enabled",
    "temporarily_disable_import_isolation",
]

ENABLE_ISOLATED_IMPORTS = True

# Temporary collections and view layers created by this module use a fixed prefix.
_IMPORT_PREFIX = "_W3_IMPORT_"


# Temporary per-process disable depth.
#
# This is intentionally simple:
# - normal code path: isolation is enabled when ENABLE_ISOLATED_IMPORTS is True
# - special code path: callers can use temporarily_disable_import_isolation()
#   around one block without having to change function signatures everywhere
_DISABLE_DEPTH = 0


def set_import_isolation_enabled(enabled: bool) -> None:
    """Set the process-wide default for isolated imports."""
    global ENABLE_ISOLATED_IMPORTS
    ENABLE_ISOLATED_IMPORTS = bool(enabled)


def import_isolation_enabled(enabled: bool = True) -> bool:
    """Return True when isolated imports should be used for this call."""
    if not enabled:
        return False
    if not ENABLE_ISOLATED_IMPORTS:
        return False
    return _DISABLE_DEPTH == 0


@contextmanager
def temporarily_disable_import_isolation():
    """Temporarily turn isolation off for one call chain.

    This is the escape hatch that keeps isolation out of unrelated APIs.
    Callers that genuinely need the legacy behavior can opt out locally without
    teaching every importer about new parameters.
    """
    global _DISABLE_DEPTH
    _DISABLE_DEPTH += 1
    try:
        yield
    finally:
        _DISABLE_DEPTH = max(0, _DISABLE_DEPTH - 1)


def is_isolated_import_context(context=None) -> bool:
    """Return True when the active Blender context is already isolated."""
    ctx = context or bpy.context
    view_layer = getattr(ctx, "view_layer", None)
    active_layer_collection = getattr(view_layer, "active_layer_collection", None) if view_layer else None
    active_collection = getattr(active_layer_collection, "collection", None)
    active_collection_name = getattr(active_collection, "name", "") or ""
    view_layer_name = getattr(view_layer, "name", "") or ""
    return active_collection_name.startswith(_IMPORT_PREFIX) or view_layer_name.startswith(_IMPORT_PREFIX)


def needs_isolation_session(context=None, enabled: bool = True) -> bool:
    """Return True when the caller should open an isolated import session."""
    if not import_isolation_enabled(enabled):
        return False
    return not is_isolated_import_context(context)


def collect_related_hierarchy_objects(root_obj):
    """Collect objects that should remain visible during an isolated import.

    Why this exists:
    - appearance and equipment imports need to see the owner armature
    - some imports need the armature parent chain intact
    - child component rigs and meshes are often referenced during import

    The helper stays intentionally conservative:
    - include ancestors so parenting and owner references still exist
    - include the whole descendant hierarchy because import code often searches
      children recursively
    """
    if root_obj is None:
        return []

    collected = []
    seen = set()

    def _append(obj):
        if obj is None:
            return
        try:
            obj_key = int(obj.as_pointer())
        except Exception:
            obj_key = id(obj)
        if obj_key in seen:
            return
        seen.add(obj_key)
        collected.append(obj)

    current = root_obj
    ancestors = []
    while current is not None:
        ancestors.append(current)
        current = getattr(current, "parent", None)
    for obj in reversed(ancestors):
        _append(obj)

    for obj in [root_obj] + list(getattr(root_obj, "children_recursive", []) or []):
        _append(obj)

    return collected


def _iter_collection_descendants(root_collection):
    if root_collection is None:
        return
    for child in getattr(root_collection, "children", []) or []:
        yield child
        yield from _iter_collection_descendants(child)


def _find_layer_collection_for_collection(layer_collection, target_collection):
    if layer_collection is None or target_collection is None:
        return None
    if getattr(layer_collection, "collection", None) == target_collection:
        return layer_collection
    for child in getattr(layer_collection, "children", []) or []:
        found = _find_layer_collection_for_collection(child, target_collection)
        if found is not None:
            return found
    return None


def _move_temp_objects_to_target(source_collection, target_collection, skip_objects=None):
    """Move imported objects out of the temp collection into the real target.

    Visible owner objects are linked into the temp collection only so the import
    code can reference them.  Those objects must not be moved or unlinked from
    their original home, so callers pass them through skip_objects.
    """
    if source_collection is None or target_collection is None:
        return []

    skip_ids = set()
    for obj in skip_objects or []:
        if obj is None:
            continue
        try:
            skip_ids.add(int(obj.as_pointer()))
        except Exception:
            skip_ids.add(id(obj))

    moved_objects = []
    for obj in list(getattr(source_collection, "all_objects", []) or []):
        if obj is None or getattr(obj, "name", None) not in bpy.data.objects:
            continue
        try:
            obj_id = int(obj.as_pointer())
        except Exception:
            obj_id = id(obj)
        if obj_id in skip_ids:
            continue
        try:
            if target_collection not in obj.users_collection:
                target_collection.objects.link(obj)
        except Exception:
            pass
        moved_objects.append(obj)

    for collection in [source_collection] + list(_iter_collection_descendants(source_collection)):
        for obj in list(getattr(collection, "objects", []) or []):
            if obj is None or getattr(obj, "name", None) not in bpy.data.objects:
                continue
            try:
                if collection in obj.users_collection:
                    collection.objects.unlink(obj)
            except Exception:
                pass

    return moved_objects


@dataclass
class ImportIsolationSession:
    """Small result object returned by isolated_import_session()."""
    context: object
    isolated: bool = False
    target_collection: object = None
    final_target_collection: object = None


@contextmanager
def isolated_import_session(context, target_collection, *, label="Import", visible_objects=None, enabled=True):
    """Run import code in a temporary collection and view layer.

    Design goals:
    - keep the feature centralized in one module
    - let import code opt in only at the public entry point
    - keep nested calls cheap by turning them into a no-op
    - keep existing import implementations unchanged as much as possible
    """
    ctx = context or bpy.context

    if not needs_isolation_session(ctx, enabled=enabled):
        yield ImportIsolationSession(
            context=ctx,
            isolated=False,
            target_collection=target_collection,
            final_target_collection=target_collection,
        )
        return

    window = getattr(ctx, "window", None)
    scene = getattr(ctx, "scene", None)
    if window is None or scene is None or target_collection is None:
        yield ImportIsolationSession(
            context=ctx,
            isolated=False,
            target_collection=target_collection,
            final_target_collection=target_collection,
        )
        return

    temp_collection = None
    temp_view_layer = None
    previous_view_layer = getattr(window, "view_layer", None)
    linked_visible_objects = []
    visible_object_ids = set()
    moved_objects = []
    hidden_state_by_id = {}

    try:
        # Create a temporary collection inside the scene root.  Imported objects
        # land here first so they are easy to separate from the rest of the scene.
        temp_collection = bpy.data.collections.new(f"{_IMPORT_PREFIX}{str(label or 'Import')[:40]}")
        scene.collection.children.link(temp_collection)

        # Create a matching temporary view layer and make only the temp
        # collection visible in that layer.  This keeps import operators focused
        # on the isolated sandbox instead of the whole scene.
        temp_view_layer = scene.view_layers.new(f"{_IMPORT_PREFIX}{str(label or 'Import')[:40]}")
        target_layer_collection = _find_layer_collection_for_collection(
            getattr(temp_view_layer, "layer_collection", None),
            temp_collection,
        )
        if target_layer_collection is None:
            raise RuntimeError(f"Could not find isolated import layer for '{temp_collection.name}'")

        for child_layer_collection in getattr(temp_view_layer.layer_collection, "children", []) or []:
            child_layer_collection.exclude = (child_layer_collection.collection != temp_collection)
        temp_view_layer.active_layer_collection = target_layer_collection

        # Link the caller-provided owner objects into the temp collection so the
        # importer can still resolve parent rigs, anchors, and references.
        for obj in visible_objects or []:
            if obj is None or getattr(obj, "name", None) not in bpy.data.objects:
                continue
            try:
                visible_object_ids.add(int(obj.as_pointer()))
            except Exception:
                visible_object_ids.add(id(obj))
            try:
                if temp_collection not in obj.users_collection:
                    temp_collection.objects.link(obj)
                    linked_visible_objects.append(obj)
            except Exception:
                pass

        # Hide scene root objects that are not part of the isolated visible set.
        # Excluding other layer collections already does most of the work; this
        # extra hide step helps for objects linked directly under scene.collection.
        for obj in list(getattr(scene.collection, "objects", []) or []):
            try:
                obj_id = int(obj.as_pointer())
            except Exception:
                obj_id = id(obj)
            if obj_id in visible_object_ids:
                continue
            try:
                obj.hide_set(True, view_layer=temp_view_layer)
            except TypeError:
                try:
                    obj.hide_set(True)
                except Exception:
                    pass
            except Exception:
                pass

        # Switch the active window to the temporary layer.  Blender operators
        # called during the import now see the isolated environment.
        window.view_layer = temp_view_layer

        yield ImportIsolationSession(
            context=bpy.context,
            isolated=True,
            target_collection=temp_collection,
            final_target_collection=target_collection,
        )
    finally:
        # Capture hide state before we move objects out of the temp collection.
        if temp_collection is not None and temp_view_layer is not None:
            for obj in list(getattr(temp_collection, "all_objects", []) or []):
                if obj is None or getattr(obj, "name", None) not in bpy.data.objects:
                    continue
                try:
                    obj_id = int(obj.as_pointer())
                except Exception:
                    obj_id = id(obj)
                try:
                    hidden_state_by_id[obj_id] = bool(obj.hide_get(view_layer=temp_view_layer))
                except TypeError:
                    try:
                        hidden_state_by_id[obj_id] = bool(obj.hide_get())
                    except Exception:
                        hidden_state_by_id[obj_id] = bool(getattr(obj, "hide_viewport", False))
                except Exception:
                    hidden_state_by_id[obj_id] = bool(getattr(obj, "hide_viewport", False))

        # Move newly imported objects into the real target collection before the
        # temporary collection is deleted.
        if temp_collection is not None and target_collection is not None:
            try:
                moved_objects = _move_temp_objects_to_target(
                    temp_collection,
                    target_collection,
                    skip_objects=linked_visible_objects,
                )
            except Exception:
                log.warning(
                    "Failed to move isolated import objects back to '%s'",
                    getattr(target_collection, "name", "?"),
                    exc_info=True,
                )

        # Restore the original view layer as soon as the import is done.
        try:
            if previous_view_layer is not None and getattr(window, "view_layer", None) != previous_view_layer:
                window.view_layer = previous_view_layer
        except Exception:
            pass

        # Re-apply the hide state that objects had inside the isolated layer.
        if previous_view_layer is not None and moved_objects:
            for obj in moved_objects:
                if obj is None or getattr(obj, "name", None) not in bpy.data.objects:
                    continue
                try:
                    obj_id = int(obj.as_pointer())
                except Exception:
                    obj_id = id(obj)
                hidden = hidden_state_by_id.get(obj_id)
                if hidden is None:
                    continue
                try:
                    obj.hide_set(hidden, view_layer=previous_view_layer)
                except TypeError:
                    try:
                        obj.hide_set(hidden)
                    except Exception:
                        pass
                except Exception:
                    pass

        # Clean up the temporary layer and collection tree.
        if temp_view_layer is not None:
            try:
                if temp_view_layer.name in [vl.name for vl in scene.view_layers]:
                    scene.view_layers.remove(temp_view_layer)
            except Exception:
                pass
        if temp_collection is not None:
            for child_collection in reversed(list(_iter_collection_descendants(temp_collection))):
                try:
                    if child_collection.name in bpy.data.collections:
                        bpy.data.collections.remove(child_collection)
                except Exception:
                    pass
            try:
                if temp_collection.name in bpy.data.collections:
                    bpy.data.collections.remove(temp_collection)
            except Exception:
                pass
