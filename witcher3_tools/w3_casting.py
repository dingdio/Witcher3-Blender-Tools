from __future__ import annotations

import re

import bpy

from .repo_paths.casting import casting_record, resolve_cast
from .repo_paths.entity_resolver import resolve_entity_repo_path
from .repo_paths.materialize import materialize_entity_repo_path
from .importers.import_cutscene import ensure_actor_custom_props


def _default_actor_label(name: str) -> str:
    label = re.sub(r"[^a-z0-9]+", "_", str(name or "").strip().lower()).strip("_")
    return label or "actor"


def _unique_actor_label(label: str) -> str:
    taken = {
        str(obj.get("cutscene_actor_name", "") or "").strip().lower()
        for obj in bpy.data.objects
    }
    if label.lower() not in taken:
        return label
    for i in range(2, 100):
        candidate = f"{label}_{i}"
        if candidate.lower() not in taken:
            return candidate
    return label


def cast_actor(name, appearance="", at=None, actor_label="", entity_namespace="", load_equipment=True):
    from .importers.import_entity import import_entity_file

    text = str(name or "").strip()
    template_path = ""
    record = None
    is_path = text.lower().endswith(".w2ent") or "\\" in text or "/" in text
    if is_path:
        template_path = text.replace("/", "\\")
        record = casting_record(template_path)
    else:
        candidates = resolve_cast(text)
        if candidates:
            template_path = candidates[0]["path"]
            record = candidates[0]["record"]
    if not template_path:
        raise ValueError(f"No casting candidate for '{name}'")

    resolved = resolve_entity_repo_path(template_path)
    repo_path = (getattr(resolved, "repo_path", "") or template_path).replace("/", "\\")
    abs_path = materialize_entity_repo_path(repo_path)
    if not abs_path:
        raise ValueError(f"Could not materialize '{repo_path}'")

    if not appearance and record:
        used = record.get("usedAppearances") or []
        appearances = record.get("appearances") or []
        appearance = used[0] if used else (appearances[0] if appearances else "")

    result = import_entity_file(
        abs_path,
        selected_appearance_name=appearance or "",
        entity_namespace=entity_namespace,
        load_appearance_equipment=bool(load_equipment),
    )
    errors = list(getattr(result, "errors", []) or [])
    actor = getattr(result, "main_object", None)
    if actor is None:
        raise ValueError(f"Entity import produced no object for '{repo_path}': {errors[:3]}")

    label_source = repo_path.rsplit("\\", 1)[-1].rsplit(".", 1)[0] if is_path else text
    label = _unique_actor_label(actor_label or _default_actor_label(label_source))
    root = getattr(result, "root_object", None) or actor
    armature = None
    stack = [actor, root]
    while stack:
        obj = stack.pop()
        if getattr(obj, "type", None) == 'ARMATURE':
            armature = obj
            break
        stack.extend(getattr(obj, "children", []) or [])
    if armature is None:
        for obj in list(getattr(result, "created_objects", None) or []):
            data = getattr(obj, "data", None)
            try:
                bpy.data.objects.remove(obj, do_unlink=True)
                if data is not None and data.users == 0:
                    bpy.data.batch_remove([data])
            except Exception:
                pass
        raise ValueError(f"'{repo_path}' imported without an armature; cutscene actors need an animated component rig")
    for holder in ({actor, armature} - {None}):
        holder["cutscene_actor_name"] = label
        holder["cutscene_actor_template"] = repo_path
        holder["cutscene_actor_appearance"] = appearance or ""
        holder["cutscene_actor_type"] = "CAT_Actor"
        ensure_actor_custom_props(holder)
    if at is not None:
        root.location = at

    info = {
        "label": label,
        "template": repo_path,
        "appearance": appearance or "",
        "record": record,
        "errors": errors,
    }
    return actor, info
