"""Build CAnimatedComponent entity data and serialize it through WolvenKit.

Meshes use CHardAttachment and CSkeletonBoneSlot records to bind CMeshComponent
instances to skeleton bones. ``trajectories_24.w2ent`` uses this structure for
Trajectory01 through Trajectory24.
"""

import base64
import json
import os
import subprocess
import tempfile

from ..rigging.attachment import (
    attachment_flags_text,
    coerce_attachment_flags,
    engine_transform_is_identity,
    normalize_engine_transform,
)

TRAJECTORY_RIG_PATH = r"animations\cutscenes\trajectory\trajectories_24.w2rig"
CUTSCENE_BEHAVIOR_PATH = r"gameplay\behaviors\cutscene_graph.w2beh"
CUTSCENE_INSTANCE_NAME = "Cutscene"
TRAJECTORY_BONE_COUNT = 24
DEFAULT_COMPONENT_NAME = "CAnimatedComponent0"


def trajectory_bone_names(count=TRAJECTORY_BONE_COUNT):
    """Return Root followed by Trajectory01 through TrajectoryN."""
    return ["Root"] + [f"Trajectory{i:02d}" for i in range(1, int(count) + 1)]


# WolvenKit JSON nodes

def _prim(t, v):
    return {"_type": t, "_value": v}


def _cname(v):
    return {"_type": "CName", "_value": v or ""}


def _new_guid_value():
    return base64.b64encode(os.urandom(16)).decode("ascii")


def _guid(value=None):
    return {"_type": "CGUID", "_value": value or _new_guid_value()}


def _ptr(ptr_type, ref):
    return {"_type": ptr_type, "_vars": {"_reference": _prim("string", ref)}}


def _handle_chunk(handle_type, ref):
    return {"_type": handle_type, "_vars": {
        "_chunkHandle": _prim("bool", True),
        "_reference": _prim("string", ref),
    }}


def _handle_import(handle_type, class_name, depot_path):
    return {"_type": handle_type, "_vars": {
        "_chunkHandle": _prim("bool", False),
        "_className": _prim("string", class_name),
        "_depotPath": _prim("string", depot_path),
        "_flags": _prim("uint16", 0),
    }}


def _arr(arr_type, elements):
    return {"_type": arr_type, "_elements": list(elements)}


def _engine_transform(pos=None, rot=None, scale=None):
    """Compact EngineTransform: only non-identity channels are emitted."""
    v = {}
    if pos:
        for axis, comp in zip("XYZ", pos):
            if comp:
                v[axis] = _prim("Float", float(comp))
    if rot:
        for name, comp in zip(("Pitch", "Yaw", "Roll"), rot):
            if comp:
                v[name] = _prim("Float", float(comp))
    if scale:
        for axis, comp in zip(("Scale_x", "Scale_y", "Scale_z"), scale):
            if comp != 1.0:
                v[axis] = _prim("Float", float(comp))
    return {"_type": "EngineTransform", "_vars": v}


def _engine_transform_value(transform=None):
    values = normalize_engine_transform(transform)
    return _engine_transform(
        pos=(values["X"], values["Y"], values["Z"]),
        rot=(values["Pitch"], values["Yaw"], values["Roll"]),
        scale=(values["Scale_x"], values["Scale_y"], values["Scale_z"]),
    )


def _behavior_slots(behavior_path):
    slot = {"_type": "SBehaviorGraphInstanceSlot", "_vars": {
        "instanceName": _cname(CUTSCENE_INSTANCE_NAME),
        "graph": _handle_import("handle:CBehaviorGraph", "CBehaviorGraph", behavior_path),
    }}
    return _arr("array:2,0,SBehaviorGraphInstanceSlot", [slot])


# Entity tree construction

def _build_component_tree(attachments, prefix, entity_idx, anim_idx, first_triplet_idx,
                          skeleton_path, behavior_path, entity_name, component_name,
                          component_guids, is_flat):
    """Build an ordered outer or flatCompiledData entity tree."""
    def key(chunk_type, idx):
        return f"{prefix}{chunk_type} #{idx}"

    flags = 0 if is_flat else 8192
    entity_key = key("CEntity", entity_idx)
    anim_key = key("CAnimatedComponent", anim_idx)

    triplets = []
    for j, _ in enumerate(attachments):
        base = first_triplet_idx + 3 * j
        triplets.append((
            key("CHardAttachment", base),
            key("CMeshComponent", base + 2),
            key("CSkeletonBoneSlot", base + 1),
        ))

    chunks = {}

    entity_vars = {
        "AttachmentsReference": _arr("array:0,0,handle:IAttachment", []),
        "AttachmentsChild": _arr("array:0,0,handle:IAttachment", []),
        "streamingDistance": _prim("Uint8", 17),
        "entityStaticFlags": _prim("EEntityStaticFlags", ""),
    }
    if is_flat:
        entity_vars["name"] = _prim("String", entity_name)
    entity_vars["Components"] = _arr("array:0,0,ptr:CComponent", [
        _ptr("ptr:CComponent", anim_key),
        *[_ptr("ptr:CComponent", mesh_key) for (_, mesh_key, _) in triplets],
    ])
    entity_vars["BufferV1"] = {"_type": "CCompressedBuffer:SEntityBufferType1", "_elements": [
        {"_type": "SEntityBufferType1", "_vars": {"ComponentName": _cname("")}}]}
    entity_vars["BufferV2"] = {"_type": "CBufferUInt32:SEntityBufferType2", "_elements": []}
    chunks[entity_key] = {
        "_type": "CEntity", "_key": entity_key,
        "_parentKey": "" if is_flat else f"CEntityTemplate #{entity_idx - 1}",
        "_flags": flags, "_vars": entity_vars,
    }

    anim_vars = {
        "guid": _guid(component_guids["animated"]),
        "AttachmentsReference": _arr("array:0,0,handle:IAttachment", []),
        "AttachmentsChild": _arr("array:0,0,handle:IAttachment", [
            _handle_chunk("handle:IAttachment", hard_key) for (hard_key, _, _) in triplets]),
        "name": _prim("String", component_name),
    }
    if is_flat:
        anim_vars["graphPositionX"] = _prim("Int16", 0)
        anim_vars["graphPositionY"] = _prim("Int16", 0)
    anim_vars["skeleton"] = _handle_import("handle:CSkeleton", "CSkeleton", skeleton_path)
    slots = _behavior_slots(behavior_path) if behavior_path else _arr(
        "array:2,0,SBehaviorGraphInstanceSlot", [])
    anim_vars["behaviorInstanceSlots"] = slots
    anim_vars["runtimeBehaviorInstanceSlots"] = (
        _behavior_slots(behavior_path) if behavior_path
        else _arr("array:2,0,SBehaviorGraphInstanceSlot", []))
    chunks[anim_key] = {
        "_type": "CAnimatedComponent", "_key": anim_key, "_parentKey": entity_key,
        "_flags": flags, "_vars": anim_vars,
    }

    for attachment_index, (att, (hard_key, mesh_key, slot_key)) in enumerate(zip(attachments, triplets)):
        hard_vars = {
            "parent": _ptr("ptr:CNode", anim_key),
            "child": _ptr("ptr:CNode", mesh_key),
            "parentSlotName": _cname(att["slot"]),
            "parentSlot": _ptr("ptr:ISlot", slot_key),
        }
        relative_transform = att.get("relative_transform")
        if not engine_transform_is_identity(relative_transform):
            hard_vars["relativeTransform"] = _engine_transform_value(relative_transform)
        flags_text = attachment_flags_text(att.get("attachment_flags", 0))
        if flags_text:
            hard_vars["attachmentFlags"] = _prim("EHardAttachmentFlags", flags_text)
        chunks[hard_key] = {
            "_type": "CHardAttachment", "_key": hard_key, "_parentKey": anim_key,
            "_flags": flags, "_vars": hard_vars,
        }

        chunks[slot_key] = {
            "_type": "CSkeletonBoneSlot", "_key": slot_key, "_parentKey": anim_key,
            "_flags": flags, "_vars": {"boneIndex": _prim("Uint32", int(att["bone_index"]))},
        }

        mesh_vars = {
            "transform": _engine_transform_value(att.get("component_transform")),
            "transformParent": _ptr("ptr:CHardAttachment", hard_key),
            "guid": _guid(component_guids["meshes"][attachment_index]),
            "AttachmentsReference": _arr("array:0,0,handle:IAttachment", [
                _handle_chunk("handle:IAttachment", hard_key)]),
            "AttachmentsChild": _arr("array:0,0,handle:IAttachment", []),
            "name": _prim("String", att.get("name") or _mesh_stem(att["mesh"])),
        }
        if is_flat:
            mesh_vars["graphPositionX"] = _prim("Int16", 0)
            mesh_vars["graphPositionY"] = _prim("Int16", 0)
            mesh_vars["drawableFlags"] = _prim("EDrawableFlags", "DF_IsVisible|DF_MissedUpdateTransform")
        mesh_vars["mesh"] = _handle_import("handle:CMesh", "CMesh", att["mesh"])
        chunks[mesh_key] = {
            "_type": "CMeshComponent", "_key": mesh_key, "_parentKey": entity_key,
            "_flags": flags, "_vars": mesh_vars,
        }

    return chunks


def _mesh_stem(mesh_path):
    return os.path.splitext(os.path.basename(str(mesh_path).replace("\\", "/")))[0]


def _imports(attachments, skeleton_path, behavior_path):
    imports = [{"_className": "CSkeleton", "_depotPath": skeleton_path, "_flags": 0}]
    if behavior_path:
        imports.append({"_className": "CBehaviorGraph", "_depotPath": behavior_path, "_flags": 0})
    seen = set()
    for att in attachments:
        mesh = att["mesh"]
        if mesh in seen:
            continue
        seen.add(mesh)
        imports.append({"_className": "CMesh", "_depotPath": mesh, "_flags": 0})
    return imports


_EMPTY_PROP_TABLE = [{"Property": {
    "className": 0, "classFlags": 0, "propertyName": 0, "propertyFlags": 0, "hash": 0}}]


def build_entity_json(attachments, skeleton_path=TRAJECTORY_RIG_PATH,
                      behavior_path=CUTSCENE_BEHAVIOR_PATH, entity_name="AnimatedComponentEntity",
                      component_name=DEFAULT_COMPONENT_NAME):
    """Build WolvenKit JSON for a CAnimatedComponent entity.

    Each attachment requires ``mesh``, ``slot``, and ``bone_index``. Optional
    fields are ``name``, ``attachment_flags``, ``relative_transform``, and
    ``component_transform``.
    """
    attachments = _normalize(attachments)
    imports = _imports(attachments, skeleton_path, behavior_path)
    component_guids = {
        "animated": _new_guid_value(),
        "meshes": [_new_guid_value() for _attachment in attachments],
    }

    outer = _build_component_tree(
        attachments, "", entity_idx=1, anim_idx=2, first_triplet_idx=3,
        skeleton_path=skeleton_path, behavior_path=behavior_path,
        entity_name=entity_name, component_name=component_name,
        component_guids=component_guids, is_flat=False)

    flat_chunks = _build_component_tree(
        attachments, "flatCompiledData::", entity_idx=0, anim_idx=1, first_triplet_idx=2,
        skeleton_path=skeleton_path, behavior_path=behavior_path,
        entity_name=entity_name, component_name=component_name,
        component_guids=component_guids, is_flat=True)

    flat = {
        "_type": "CR2W", "_extension": "flatCompiledData::",
        "_imports": imports, "_properties": _EMPTY_PROP_TABLE,
        "_buffers": [], "_embedded": [], "_chunks": flat_chunks,
    }

    template_chunk = {
        "_type": "CEntityTemplate", "_key": "CEntityTemplate #0",
        "_parentKey": "", "_flags": 8192, "_vars": {
            "properOverrides": _prim("Bool", True),
            "entityObject": _ptr("ptr:CEntity", "CEntity #1"),
            "flatCompiledData": flat,
            "cookedEffectsVersion": _prim("Uint32", 1),
        },
    }

    chunks = {"CEntityTemplate #0": template_chunk}
    chunks.update(outer)
    return {
        "_type": "CR2W", "_extension": "", "_imports": imports,
        "_properties": _EMPTY_PROP_TABLE, "_buffers": [], "_embedded": [], "_chunks": chunks,
    }


def _normalize(attachments):
    out = []
    for att in attachments:
        mesh = str(att.get("mesh", "")).replace("/", "\\").strip("\\")
        if not mesh:
            raise ValueError("CHardAttachment is missing a mesh depot path")
        slot = str(att.get("slot", "")).strip()
        if not slot:
            raise ValueError("CHardAttachment is missing a parentSlotName (bone)")
        relative_transform = att.get("relative_transform")
        if relative_transform is None and att.get("relative_rot") is not None:
            pitch, yaw, roll = att.get("relative_rot")
            relative_transform = {"Pitch": pitch, "Yaw": yaw, "Roll": roll}
        component_transform = att.get("component_transform")
        if component_transform is None and att.get("scale") is not None:
            sx, sy, sz = att.get("scale")
            component_transform = {"Scale_x": sx, "Scale_y": sy, "Scale_z": sz}
        out.append({
            "mesh": mesh,
            "slot": slot,
            "bone_index": int(att.get("bone_index", 0)),
            "name": att.get("name"),
            "attachment_flags": coerce_attachment_flags(att.get("attachment_flags", 0)),
            "relative_transform": normalize_engine_transform(relative_transform),
            "component_transform": normalize_engine_transform(component_transform),
        })
    return out


def generate_entity(attachments, out_w2ent_path, wolvenkit_exe,
                    skeleton_path=TRAJECTORY_RIG_PATH, behavior_path=CUTSCENE_BEHAVIOR_PATH,
                    entity_name="AnimatedComponentEntity", component_name=DEFAULT_COMPONENT_NAME):
    """Serialize a CAnimatedComponent .w2ent and return its path."""
    if not wolvenkit_exe or not os.path.isfile(wolvenkit_exe):
        raise RuntimeError("WolvenKit CLI is required to export entities; set its path in "
                           "the addon preferences.")
    data = build_entity_json(
        attachments, skeleton_path=skeleton_path, behavior_path=behavior_path,
        entity_name=entity_name, component_name=component_name)

    out_dir = os.path.dirname(out_w2ent_path)
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)

    json_fd, json_path = tempfile.mkstemp(suffix=".json", prefix="anim_comp_")
    try:
        with os.fdopen(json_fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        result = subprocess.run(
            [wolvenkit_exe, "--input", json_path, "--output", out_w2ent_path, "--json2cr2w"],
            capture_output=True, text=True)
        if result.returncode != 0 or not os.path.isfile(out_w2ent_path):
            raise RuntimeError("WolvenKit json2cr2w failed:\n"
                               + (result.stderr or result.stdout or "no output"))
    finally:
        try:
            os.remove(json_path)
        except OSError:
            pass
    return out_w2ent_path
