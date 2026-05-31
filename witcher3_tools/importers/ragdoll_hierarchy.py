import logging
import re

log = logging.getLogger(__name__)

# Trailing "_NN" segment used by W2 dangle bone chains, e.g. Hair_1_01 / ponytail_03.
_SEGMENT_RE = re.compile(r"^(.*?)_(\d+)$")
_ANCHOR_FALLBACKS = ("head", "neck", "torso2", "torso", "pelvis")

def derive_ragdoll_bone_parents(joint_names, anchor_bone="", havok_parents=None):
    names = list(joint_names)
    nameset = set(names)

    if havok_parents and len(havok_parents) == len(names):
        out = {}
        ok = True
        for i, nm in enumerate(names):
            p = havok_parents[i]
            if isinstance(p, int) and 0 <= p < len(names) and p != i:
                out[nm] = names[p]
            elif isinstance(p, int) and p < 0:
                out[nm] = None  # root
            else:
                ok = False
                break
        if ok:
            return out, "havok"

    anchor = anchor_bone if anchor_bone in nameset else next(
        (a for a in _ANCHOR_FALLBACKS if a in nameset), None
    )
    groups = {}  # stem -> [(num, name), ...]
    for nm in names:
        m = _SEGMENT_RE.match(nm)
        if m:
            groups.setdefault(m.group(1), []).append((int(m.group(2)), nm))

    out = {nm: None for nm in names}
    for stem, segs in groups.items():
        segs.sort()
        prev = None
        for _num, nm in segs:
            if prev is None:
                # First (lowest) segment anchors to the body bone, never itself.
                out[nm] = anchor if anchor and anchor != nm else None
            else:
                out[nm] = prev
            prev = nm
    return out, "naming"


def apply_ragdoll_hierarchy(armature_obj, anchor_bone="", havok_parents=None):
    """Parent the dangle bones of an imported ragdoll armature in-place.
    """
    import bpy

    if armature_obj is None or getattr(armature_obj, "type", "") != "ARMATURE":
        return 0

    joint_names = [b.name for b in armature_obj.data.bones]
    parents, mode = derive_ragdoll_bone_parents(joint_names, anchor_bone, havok_parents)
    if not any(parents.values()):
        return 0

    prev_active = bpy.context.view_layer.objects.active
    prev_mode = armature_obj.mode if armature_obj == prev_active else None
    bpy.context.view_layer.objects.active = armature_obj
    linked = 0
    try:
        bpy.ops.object.mode_set(mode="EDIT")
        edit_bones = armature_obj.data.edit_bones
        for child_name, parent_name in parents.items():
            if not parent_name:
                continue
            child = edit_bones.get(child_name)
            parent = edit_bones.get(parent_name)
            if child is None or parent is None or child == parent:
                continue
            if child.parent is not None:
                continue
            child.parent = parent
            child.use_connect = False
            linked += 1
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")
        if prev_active is not None:
            bpy.context.view_layer.objects.active = prev_active

    log.info(
        "Applied %s ragdoll hierarchy to '%s': %d parent links (anchor=%s)",
        mode, armature_obj.name, linked, anchor_bone or "<auto>",
    )
    return linked


def store_ragdoll_metadata(armature_obj, ragdoll_meta):
    """Persist W2 ragdoll physics metadata onto the armature for later simulation.
    """
    import json

    if armature_obj is None or not ragdoll_meta:
        return
    try:
        armature_obj["witcher_w2_ragdoll"] = json.dumps(ragdoll_meta)
    except Exception:
        log.debug("Failed to serialize ragdoll metadata for %s", armature_obj.name, exc_info=True)
        return
    for key, prop in (
        ("base_body", "witcher_w2_ragdoll_base_body"),
        ("skeleton_high", "witcher_w2_ragdoll_skeleton"),
        ("import_file", "witcher_w2_ragdoll_import_file"),
    ):
        value = ragdoll_meta.get(key)
        if value:
            armature_obj[prop] = str(value)
