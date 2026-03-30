import logging
import os
import json
from collections import Counter
from ..CR2W import read_json_w3
from ..CR2W import w3_types
from ..importers import import_entity
from ..CR2W.dc_anims import load_bin_cutscene
from ..CR2W.common_blender import repo_file

log = logging.getLogger(__name__)

def loadCutsceneFile(filename):
    dirpath, file = os.path.split(filename)
    basename, ext = os.path.splitext(file)
    if ext.lower() in ('.json'):
        with open(filename) as file:
            return read_json_w3.Read_CCutsceneTemplate(json.loads(file.read()))
    elif ext.lower().endswith('.w2cutscene'):
        return load_bin_cutscene(filename)
    else:
        return None

import bpy
from .import_anims import NewW2ANIMSListItem#, set_global_set #!USE NEW METHOD

def _normalize_repo_path(path):
    return str(path or "").replace("/", "\\").lstrip("\\")

def split_cutscene_animation_name(anim_name):
    full_name = str(anim_name or "").strip()
    parts = full_name.split(":", 2)
    if len(parts) >= 3:
        actor_name, component_name, display_name = parts
    elif len(parts) == 2:
        actor_name, display_name = parts
        component_name = ""
    else:
        actor_name = ""
        component_name = ""
        display_name = full_name
    return actor_name, component_name, display_name

def _iter_scene_armatures():
    for obj in bpy.context.scene.objects:
        if obj.type == 'ARMATURE':
            yield obj

def _get_armature_repo_path(obj):
    try:
        rig_settings = getattr(obj.data, "witcherui_RigSettings", None)
    except Exception:
        rig_settings = None
    return _normalize_repo_path(getattr(rig_settings, "repo_path", "") or "")

def _find_cutscene_actor_by_name(actor_name):
    actor_name = str(actor_name or "").strip()
    if not actor_name:
        return None
    for obj in _iter_scene_armatures():
        if str(obj.get("cutscene_actor_name", "") or "").strip() == actor_name:
            return obj
    return None

def _find_actor_by_repo_path(repo_path):
    repo_path = _normalize_repo_path(repo_path)
    if not repo_path:
        return None
    for obj in _iter_scene_armatures():
        if len(obj.name) > 4 and obj.name[-4] == ".":
            continue
        if _get_armature_repo_path(obj) == repo_path:
            return obj
    return None

def find_existing_cutscene_actor(actor_name="", repo_path="", duplicate_count=1):
    actor_obj = _find_cutscene_actor_by_name(actor_name)
    if actor_obj is not None:
        return actor_obj
    if int(duplicate_count or 0) <= 1:
        return _find_actor_by_repo_path(repo_path)
    return None

def check_if_actor_already_in_scene(repo_path):
    return find_existing_cutscene_actor(repo_path=repo_path) or False

def _actor_template_counts(actor_defs):
    return Counter(
        _normalize_repo_path(getattr(actor, "template", "") or "")
        for actor in (actor_defs or [])
        if _normalize_repo_path(getattr(actor, "template", "") or "")
    )

def _tag_cutscene_actor(actor_obj, actor):
    if actor_obj is None:
        return
    actor_name = str(getattr(actor, "name", "") or "").strip()
    if actor_name:
        actor_obj["cutscene_actor_name"] = actor_name
    actor_obj["cutscene_actor_template"] = _normalize_repo_path(getattr(actor, "template", "") or "")
    actor_obj["cutscene_actor_type"] = str(getattr(actor, "type", "") or "CAT_Actor")
    actor_obj["cutscene_component"] = "Root"
    appearance_name = str(getattr(actor, "appearance", "") or "").strip()
    if appearance_name:
        actor_obj["cutscene_actor_appearance"] = appearance_name

def _resolve_cutscene_actor_appearance(entity, preferred_name=""):
    appearances = list(getattr(entity, "appearances", None) or [])
    if not appearances:
        return None, -1, ""

    preferred_name = str(preferred_name or "").strip()
    if preferred_name:
        for idx, appearance in enumerate(appearances):
            appearance_name = str(getattr(appearance, "name", "") or "").strip()
            if appearance_name == preferred_name:
                return appearance, idx, appearance_name

    first_appearance = appearances[0]
    return first_appearance, 0, str(getattr(first_appearance, "name", "") or "").strip()

def _has_loaded_appearance_group(actor_obj, appearance_name):
    appearance_name = str(appearance_name or "").strip()
    if actor_obj is None or not appearance_name:
        return False
    for child in getattr(actor_obj, "children", []):
        if getattr(child, "type", "") != 'EMPTY':
            continue
        child_app_name = str(child.get("witcher_app_name", "") or child.name or "").strip()
        if child_app_name == appearance_name:
            return True
    return False

def _ensure_cutscene_actor_appearance(actor_obj, preferred_name=""):
    if actor_obj is None or getattr(actor_obj, "type", "") != 'ARMATURE':
        return False, ""

    rig_settings = getattr(getattr(actor_obj, "data", None), "witcherui_RigSettings", None)
    if rig_settings is None:
        return False, ""

    entity, _entity_data = import_entity.get_rig_entity_state(rig_settings)
    if entity is None:
        return False, ""

    selected_appearance, app_idx, resolved_name = _resolve_cutscene_actor_appearance(entity, preferred_name)
    if selected_appearance is None or app_idx < 0:
        return False, ""

    try:
        rig_settings.app_list_index = app_idx
    except Exception:
        pass

    if _has_loaded_appearance_group(actor_obj, resolved_name):
        return True, resolved_name

    try:
        import_entity.import_app(bpy.context, selected_appearance, entity, actor_obj)
        try:
            import_entity._focus_main_armature(bpy.context, actor_obj)
        except Exception:
            pass
        return True, resolved_name
    except Exception:
        log.exception(
            "Failed to apply cutscene appearance '%s' on actor '%s'",
            resolved_name,
            getattr(actor_obj, "name", "<unknown>"),
        )
        return False, resolved_name

def _estimate_animation_frame_count(node):
    animation = getattr(node, "animation", None)
    frame_count = int(getattr(getattr(animation, "animBuffer", None), "numFrames", 0) or 0)
    if frame_count > 0:
        return frame_count

    duration = float(getattr(animation, "duration", 0.0) or 0.0)
    fps = float(getattr(animation, "framesPerSecond", 30.0) or 30.0)
    estimated = int(round(duration * fps))
    return max(1, estimated)

def _auto_apply_cutscene_animations(filename, cutscene_template, actor_objects_by_name,
                                    selected_animation_indices=None, actor_repo_paths_by_name=None):
    from ..ui.ui_anims_list import load_anim_into_scene

    selected_animation_indices = None if selected_animation_indices is None else {int(idx) for idx in selected_animation_indices}
    actor_repo_paths_by_name = dict(actor_repo_paths_by_name or {})
    actor_frame_offsets = {}
    applied_count = 0

    for idx, node in enumerate(getattr(cutscene_template, "animations", None) or []):
        if selected_animation_indices is not None and idx not in selected_animation_indices:
            continue

        anim_name = str(getattr(getattr(node, "animation", None), "name", "") or "")
        actor_name, _component_name, _display_name = split_cutscene_animation_name(anim_name)

        actor_obj = None
        if actor_name:
            actor_obj = (
                actor_objects_by_name.get(actor_name)
                or _find_cutscene_actor_by_name(actor_name)
                or _find_actor_by_repo_path(actor_repo_paths_by_name.get(actor_name, ""))
            )
        elif len(actor_objects_by_name) == 1:
            actor_obj = next(iter(actor_objects_by_name.values()))

        if actor_obj is None:
            log.info("Skipping cutscene animation '%s': no matching actor found in scene.", anim_name or idx)
            continue

        frame_key = actor_name or getattr(actor_obj, "name", "")
        at_frame = int(actor_frame_offsets.get(frame_key, 0) or 0)
        try:
            load_anim_into_scene(
                bpy.context,
                anim_name,
                filename,
                actor_obj,
                NLA_track="cutscene_import",
                at_frame=at_frame,
            )
            applied_count += 1
        except Exception:
            log.exception(
                "Failed to auto-apply cutscene animation '%s' on actor '%s'",
                anim_name or idx,
                getattr(actor_obj, "name", "<unknown>"),
            )
            continue

        actor_frame_offsets[frame_key] = at_frame + _estimate_animation_frame_count(node)

    return applied_count

def collect_cutscene_preview(filename):
    cutscene = loadCutsceneFile(filename)
    if cutscene is None:
        return None, [], []

    actor_defs = list(getattr(cutscene, "SCutsceneActorDefs", None) or [])
    template_counts = _actor_template_counts(actor_defs)
    actor_items = []
    for idx, actor in enumerate(actor_defs):
        actor_name = str(getattr(actor, "name", "") or "").strip()
        template_path = _normalize_repo_path(getattr(actor, "template", "") or "")
        appearance_name = str(getattr(actor, "appearance", "") or "").strip()
        display_name = actor_name or os.path.splitext(os.path.basename(template_path))[0] or f"Actor {idx + 1}"
        existing = find_existing_cutscene_actor(
            actor_name=actor_name,
            repo_path=template_path,
            duplicate_count=template_counts.get(template_path, 0),
        )
        actor_items.append({
            "source_index": idx,
            "label": display_name,
            "actor_name": actor_name,
            "template_path": template_path,
            "appearance_name": appearance_name,
            "actor_type": str(getattr(actor, "type", "") or ""),
            "use_mimic": bool(getattr(actor, "useMimic", False)),
            "already_in_scene": bool(existing),
        })

    animation_items = []
    for idx, node in enumerate(getattr(cutscene, "animations", None) or []):
        animation = getattr(node, "animation", None)
        full_name = str(getattr(animation, "name", "") or f"Animation {idx + 1}")
        actor_name, component_name, display_name = split_cutscene_animation_name(full_name)
        animation_items.append({
            "source_index": idx,
            "full_name": full_name,
            "display_name": display_name or full_name,
            "actor_name": actor_name,
            "component_name": component_name,
            "frames_per_second": float(getattr(animation, "framesPerSecond", 0.0) or 0.0),
            "num_frames": int(getattr(getattr(animation, "animBuffer", None), "numFrames", 0) or 0),
            "duration": float(getattr(animation, "duration", 0.0) or 0.0),
        })

    return cutscene, actor_items, animation_items

def import_w3_cutscene(filename, selected_actor_indices=None, selected_animation_indices=None,
                      auto_apply_selected_animations=False):
    CCutsceneTemplate = loadCutsceneFile(filename)
    if CCutsceneTemplate is None:
        return None

    context = bpy.context
    treeList = context.scene.witcher_w2cutscene_list
    treeList.clear()
    context.scene.witcher_loaded_w2cutscene_path = filename

    selected_actor_indices = None if selected_actor_indices is None else {int(idx) for idx in selected_actor_indices}
    selected_animation_indices = None if selected_animation_indices is None else {int(idx) for idx in selected_animation_indices}
    actor_defs = list(getattr(CCutsceneTemplate, "SCutsceneActorDefs", None) or [])
    actor_objects_by_name = {}
    actor_repo_paths_by_name = {
        str(getattr(actor, "name", "") or "").strip(): _normalize_repo_path(getattr(actor, "template", "") or "")
        for actor in actor_defs
        if str(getattr(actor, "name", "") or "").strip()
    }

    for idx, node in enumerate(getattr(CCutsceneTemplate, "animations", None) or []):
        if selected_animation_indices is not None and idx not in selected_animation_indices:
            continue
        NewW2ANIMSListItem(treeList, node)

    template_counts = _actor_template_counts(actor_defs)
    actor:w3_types.SCutsceneActorDef
    for idx, actor in enumerate(actor_defs):
        if selected_actor_indices is not None and idx not in selected_actor_indices:
            continue

        actor_name = str(getattr(actor, "name", "") or "").strip()
        template_path = _normalize_repo_path(getattr(actor, "template", "") or "")
        preferred_appearance_name = str(getattr(actor, "appearance", "") or "").strip()
        actor_obj = find_existing_cutscene_actor(
            actor_name=actor_name,
            repo_path=template_path,
            duplicate_count=template_counts.get(template_path, 0),
        )
        if not actor_obj and template_path:
            try:
                actor_obj = import_entity.import_ent_template(
                    repo_file(template_path),
                    load_face_poses=bool(getattr(actor, "useMimic", False)),
                    import_apperance=1,
                    selected_appearance_name=preferred_appearance_name,
                )
            except Exception:
                log.exception("Failed to import cutscene actor '%s' from '%s'", actor_name or idx, template_path)
                actor_obj = None
        if actor_obj:
            _ensure_cutscene_actor_appearance(actor_obj, preferred_appearance_name)
            _tag_cutscene_actor(actor_obj, actor)
            if actor_name:
                actor_objects_by_name[actor_name] = actor_obj

    auto_applied_animation_count = 0
    if auto_apply_selected_animations:
        auto_applied_animation_count = _auto_apply_cutscene_animations(
            filename,
            CCutsceneTemplate,
            actor_objects_by_name,
            selected_animation_indices=selected_animation_indices,
            actor_repo_paths_by_name=actor_repo_paths_by_name,
        )

    try:
        CCutsceneTemplate.auto_applied_animation_count = int(auto_applied_animation_count)
    except Exception:
        pass

    return CCutsceneTemplate

