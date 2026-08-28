"""Build entity templates (.w2ent): CAnimatedComponent + hard attachments and/or static meshes."""

import base64
import os
import struct

from ..rigging.attachment import (
    attachment_flag_names,
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
    skeleton_path, behavior_path = _component_paths(skeleton_path, behavior_path)
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


def _depot_path(value, what, ext=""):
    """Validate a game-relative depot path."""
    raw = str(value or "").replace("/", "\\")
    path = raw.strip("\\")
    if not path:
        raise ValueError(f"{what} is missing a depot path")
    # check the raw value: stripping first would turn a UNC path into a plausible relative one
    if (os.path.splitdrive(raw)[0] or raw.startswith("\\\\")
            or any(part in ("", ".", "..") for part in path.split("\\"))):
        raise ValueError(f"{what} path must be game-relative: {value!r}")
    if ext and not path.lower().endswith(ext):
        raise ValueError(f"{what} path must end with {ext}: {value!r}")
    return path


def _component_paths(skeleton_path, behavior_path):
    skeleton = _depot_path(skeleton_path, "CSkeleton", ".w2rig") if skeleton_path else skeleton_path
    behavior = _depot_path(behavior_path, "CBehaviorGraph", ".w2beh") if behavior_path else behavior_path
    return skeleton, behavior


def _normalize_static(meshes):
    out = []
    for item in meshes or ():
        mesh = _depot_path(item.get("mesh", ""), "CStaticMeshComponent", ".w2mesh")
        out.append({
            "mesh": mesh,
            "name": item.get("name"),
            "transform": normalize_engine_transform(item.get("transform")),
        })
    return out


def _normalize(attachments):
    out = []
    for att in attachments:
        mesh = _depot_path(att.get("mesh", ""), "CHardAttachment", ".w2mesh")
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


ENTITY_HEADER_VERSION = 162


def _build_entity_tree(attachments, skeleton_path, behavior_path, entity_name,
                       component_name, guids, is_flat, static_meshes=()):
    from . import cr2w_writer
    from .CR2W_types import PROPERTY, EngineTransform
    from .anims_builder import (
        _add_chunk,
        _init_cr2w,
        _make_cname_prop,
        _make_handle,
        _make_import_handle,
        _make_string_prop,
    )

    cr2w = _init_cr2w(ENTITY_HEADER_VERSION)
    base = 0 if is_flat else 1
    has_anim = bool(skeleton_path)
    ENTITY = base
    ANIM = base + 1 if has_anim else None
    first_triplet = base + (2 if has_anim else 1)
    hard_idx = lambda i: first_triplet + 3 * i
    slot_idx = lambda i: first_triplet + 1 + 3 * i
    mesh_idx = lambda i: first_triplet + 2 + 3 * i
    static_idx = lambda i: first_triplet + 3 * len(attachments) + i

    def ptr(name, idx, ptr_type):
        h = _make_handle(cr2w, idx, ptr_type)
        return PROPERTY(CR2WFILE=cr2w, Handles=[h], elements=[h], theName=name, theType=ptr_type)

    def import_prop(name, class_name, depot_path, handle_type):
        h = _make_import_handle(cr2w, class_name, depot_path, handle_type)
        return PROPERTY(CR2WFILE=cr2w, Handles=[h], elements=[h], theName=name, theType=handle_type)

    def guid_prop(key):
        return PROPERTY(theName="guid", theType="CGUID", Bytes=guids[key])

    def int16_prop(name, value=0):
        return PROPERTY(Value=int(value), theName=name, theType="Int16")

    class CEnumShim:
        strings = []

    def flags_prop(name, enum_type, values):
        enum_obj = CEnumShim()
        enum_obj.strings = list(values)
        return PROPERTY(theName=name, theType=enum_type, Index=enum_obj)

    def transform_prop(name, values):
        et = EngineTransform()
        for key, value in values.items():
            setattr(et, key, float(value))
        return PROPERTY(theName=name, theType="EngineTransform", EngineTransform=et)

    template_chunk = None
    if not is_flat:
        _, template_chunk = _add_chunk(cr2w, "CEntityTemplate", [
            PROPERTY(Value=True, theName="properOverrides", theType="Bool"),
            ptr("entityObject", ENTITY, "ptr:CEntity"),
            PROPERTY(Value=1, theName="cookedEffectsVersion", theType="Uint32"),
        ])

    entity_props = [
        PROPERTY(Value=17, theName="streamingDistance", theType="Uint8"),
        flags_prop("entityStaticFlags", "EEntityStaticFlags", []),
    ]
    if is_flat:
        entity_props.append(_make_string_prop("name", entity_name))
    _, entity_chunk = _add_chunk(cr2w, "CEntity", entity_props)

    def behavior_slots(prop_name):
        slots = []
        if behavior_path:
            slots.append(PROPERTY(
                theName="SBehaviorGraphInstanceSlot", theType="SBehaviorGraphInstanceSlot",
                More=[
                    _make_cname_prop("instanceName", CUTSCENE_INSTANCE_NAME),
                    import_prop("graph", "CBehaviorGraph", behavior_path, "handle:CBehaviorGraph"),
                ],
            ))
        return PROPERTY(
            theName=prop_name, theType="array:2,0,SBehaviorGraphInstanceSlot",
            elements=slots,
        )

    anim_chunk = None
    if has_anim:
        anim_props = [
            guid_prop("anim"),
            _make_string_prop("name", component_name),
        ]
        if is_flat:
            anim_props += [int16_prop("graphPositionX"), int16_prop("graphPositionY")]
        anim_props += [
            import_prop("skeleton", "CSkeleton", skeleton_path, "handle:CSkeleton"),
            behavior_slots("behaviorInstanceSlots"),
            behavior_slots("runtimeBehaviorInstanceSlots"),
        ]
        _, anim_chunk = _add_chunk(cr2w, "CAnimatedComponent", anim_props)

    mesh_chunks = []
    for i, att in enumerate(attachments):
        hard_props = [
            ptr("parent", ANIM, "ptr:CNode"),
            ptr("child", mesh_idx(i), "ptr:CNode"),
            _make_cname_prop("parentSlotName", att["slot"]),
            ptr("parentSlot", slot_idx(i), "ptr:ISlot"),
        ]
        if not engine_transform_is_identity(att["relative_transform"]):
            hard_props.append(transform_prop("relativeTransform", att["relative_transform"]))
        flag_names = attachment_flag_names(att["attachment_flags"])
        if flag_names:
            hard_props.append(flags_prop("attachmentFlags", "EHardAttachmentFlags", flag_names))
        _add_chunk(cr2w, "CHardAttachment", hard_props)
        _add_chunk(cr2w, "CSkeletonBoneSlot", [
            PROPERTY(Value=int(att["bone_index"]), theName="boneIndex", theType="Uint32"),
        ])
        mesh_props = [
            transform_prop("transform", att["component_transform"]),
            ptr("transformParent", hard_idx(i), "ptr:CHardAttachment"),
            guid_prop(f"mesh{i}"),
            _make_string_prop("name", att.get("name") or _mesh_stem(att["mesh"])),
        ]
        if is_flat:
            mesh_props += [
                int16_prop("graphPositionX"), int16_prop("graphPositionY"),
                flags_prop("drawableFlags", "EDrawableFlags",
                           ["DF_IsVisible", "DF_MissedUpdateTransform"]),
            ]
        mesh_props.append(import_prop("mesh", "CMesh", att["mesh"], "handle:CMesh"))
        _, mesh_chunk = _add_chunk(cr2w, "CMeshComponent", mesh_props)
        mesh_chunks.append(mesh_chunk)

    for i, sm in enumerate(static_meshes):
        static_props = []
        if not engine_transform_is_identity(sm["transform"]):
            static_props.append(transform_prop("transform", sm["transform"]))
        static_props += [
            guid_prop(f"static{i}"),
            _make_string_prop("name", sm.get("name") or _mesh_stem(sm["mesh"])),
        ]
        if is_flat:
            static_props += [int16_prop("graphPositionX"), int16_prop("graphPositionY")]
        static_props.append(import_prop("mesh", "CMesh", sm["mesh"], "handle:CMesh"))
        _, static_chunk = _add_chunk(cr2w, "CStaticMeshComponent", static_props)
        static_chunk.postPropsData = struct.pack("<II", 0, 0)

    # v162 CNode/CEntity tails carry attachment refs and the component list.
    if template_chunk is not None:
        template_chunk.postPropsData = struct.pack("<I", 0)
    components = ([ANIM] if has_anim else [])
    components += [mesh_idx(i) for i in range(len(attachments))]
    components += [static_idx(i) for i in range(len(static_meshes))]
    entity_tail = struct.pack("<II", 0, 0)
    entity_tail += cr2w_writer._write_vlq_count(len(components))
    for idx in components:
        entity_tail += struct.pack("<i", idx + 1)
    entity_tail += struct.pack("<H", 0)
    entity_chunk.postPropsData = entity_tail

    if anim_chunk is not None:
        anim_tail = struct.pack("<II", 0, len(attachments))
        for i in range(len(attachments)):
            anim_tail += struct.pack("<i", hard_idx(i) + 1)
        anim_chunk.postPropsData = anim_tail

    for i, mesh_chunk in enumerate(mesh_chunks):
        mesh_chunk.postPropsData = struct.pack("<Ii", 1, hard_idx(i) + 1) + struct.pack("<I", 0)

    chunk_flags = 0 if is_flat else 8192
    layout = [] if is_flat else [0]
    layout.append(0 if is_flat else ENTITY)
    if has_anim:
        layout.append(ENTITY + 1)
    for _i in range(len(attachments)):
        layout += [ANIM + 1, ANIM + 1, ENTITY + 1]
    layout += [ENTITY + 1] * len(static_meshes)
    for idx, parent in enumerate(layout):
        cr2w.CR2WExport[idx].parentID = parent
        cr2w.CR2WExport[idx].objectFlags = chunk_flags

    return cr2w, template_chunk


def build_entity_cr2w(attachments, skeleton_path=TRAJECTORY_RIG_PATH,
                      behavior_path=CUTSCENE_BEHAVIOR_PATH,
                      entity_name="AnimatedComponentEntity",
                      component_name=DEFAULT_COMPONENT_NAME, static_meshes=()):
    """Build a native .w2ent CR2W tree."""
    from .CR2W_types import PROPERTY
    from . import cr2w_writer

    attachments = _normalize(attachments)
    static_meshes = _normalize_static(static_meshes)
    skeleton_path, behavior_path = _component_paths(skeleton_path, behavior_path)
    if attachments and not skeleton_path:
        raise ValueError("CHardAttachments require a skeleton_path")
    guids = {"anim": os.urandom(16)}
    for i in range(len(attachments)):
        guids[f"mesh{i}"] = os.urandom(16)
    for i in range(len(static_meshes)):
        guids[f"static{i}"] = os.urandom(16)

    flat_cr2w, _ = _build_entity_tree(
        attachments, skeleton_path, behavior_path, entity_name, component_name,
        guids, is_flat=True, static_meshes=static_meshes)
    flat_bytes = cr2w_writer._build_cr2w_bytes(flat_cr2w)

    outer_cr2w, template_chunk = _build_entity_tree(
        attachments, skeleton_path, behavior_path, entity_name, component_name,
        guids, is_flat=False, static_meshes=static_meshes)
    template_chunk.PROPS.insert(2, PROPERTY(
        theName="flatCompiledData", theType="array:2,0,Uint8", RawBytes=flat_bytes,
    ))
    return outer_cr2w


def generate_entity(attachments, out_w2ent_path,
                    skeleton_path=TRAJECTORY_RIG_PATH, behavior_path=CUTSCENE_BEHAVIOR_PATH,
                    entity_name="AnimatedComponentEntity", component_name=DEFAULT_COMPONENT_NAME,
                    static_meshes=()):
    from . import cr2w_writer

    cr2w = build_entity_cr2w(
        attachments, skeleton_path=skeleton_path, behavior_path=behavior_path,
        entity_name=entity_name, component_name=component_name, static_meshes=static_meshes)
    cr2w_writer.write_w2ent(cr2w, out_w2ent_path)
    return out_w2ent_path
