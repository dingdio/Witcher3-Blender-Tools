"""Build a .w2rig (CSkeleton) CR2W file from a bone list."""
import logging
import struct

from .CR2W_helpers import Enums
from .CR2W_types import PROPERTY
from .anims_builder import _add_chunk, _init_cr2w, _make_cname_prop, _make_enum_prop, _make_handle, _make_string_prop
from .prop_utils import read_cname_prop, read_enum_prop, read_string_prop

log = logging.getLogger(__name__)

OF_INLINED = 8
SKELETON_ARRAYS = ("bones", "tracks", "parentIndices")


def build_w2rig(bones, tracks=(), source_cr2w=None):
    """bones: [{name, parent, pos (x,y,z), rot (x,y,z,w), scale (x,y,z)}], parents before children.

    source_cr2w: parsed .w2rig whose extra properties (LOD, streaming bone, control rig,
    teleport detector, ...) are carried over with bone indices re-resolved by name.
    """
    names = [b["name"] for b in bones]
    lowered = [n.lower() for n in names]
    dupes = sorted({n for n, l in zip(names, lowered) if lowered.count(l) > 1})
    if dupes:
        raise ValueError(f"Duplicate bone names: {dupes}")
    for i, b in enumerate(bones):
        if not (-1 <= b["parent"] < i):
            raise ValueError(f"Bone '{b['name']}' parent index {b['parent']} must precede it")

    cr2w = _init_cr2w()
    _, skel = _add_chunk(cr2w, "CSkeleton", [])
    props = _carry_over(cr2w, source_cr2w, names) if source_cr2w is not None else []
    props.append(PROPERTY(theName="bones", theType="array:2,0,SSkeletonBone",
                          elements=[_named_struct("SSkeletonBone", n) for n in names]))
    if tracks:
        props.append(PROPERTY(theName="tracks", theType="array:2,0,SSkeletonTrack",
                              elements=[_named_struct("SSkeletonTrack", t) for t in tracks]))
    props.append(PROPERTY(theName="parentIndices", theType="array:2,0,Int16",
                          elements=[PROPERTY(Value=int(b["parent"]), theType="Int16") for b in bones]))
    skel.PROPS = props

    # Reference pose follows the property list (CSkeleton::OnSerialize): T.xyzw R.xyzw S.xyzw per bone.
    pose = bytearray()
    for b in bones:
        w = 0.0 if b["parent"] == -1 else 1.0
        pose += struct.pack("<12f", *b["pos"], w, *b["rot"], *b["scale"], w)
    skel.postPropsData = bytes(pose)
    return cr2w


def _named_struct(struct_type, name):
    return PROPERTY(theName=struct_type, theType=struct_type, More=[
        _make_string_prop("name", name, theType="StringAnsi"),
        _make_cname_prop("nameAsCName", name),
    ])


def _carry_over(cr2w, src, names):
    index = {n.lower(): i for i, n in enumerate(names)}
    chunks = src.CHUNKS.CHUNKS
    skel = next((c for c in chunks if c.name == "CSkeleton"), None)
    props = []
    for p in skel.PROPS if skel is not None else []:
        if p.theName in SKELETON_ARRAYS or p.theType.startswith("array:"):
            continue
        if p.theType.startswith("ptr:"):
            ref = int(getattr(p, "Value", 0) or 0)
            if ref <= 0:
                continue
            child = chunks[ref - 1]
            triples = _remap([_typed(q) for q in child.PROPS], index)
            idx, _ = _add_chunk(cr2w, child.name, [_make_prop(cr2w, n, t, v) for n, t, v in triples])
            cr2w.CR2WExport[idx].parentID = 1
            cr2w.CR2WExport[idx].objectFlags = OF_INLINED
            handle = _make_handle(cr2w, idx, p.theType)
            props.append(PROPERTY(CR2WFILE=cr2w, Handles=[handle], elements=[handle],
                                  theName=p.theName, theType=p.theType))
            continue
        name, t, v = _typed(p)
        if t == "CName" and str(v).lower() not in index:
            log.warning("Dropping %s: bone '%s' is not in the exported rig", name, v)
            continue
        if name == "lodBoneNum_1":
            v = min(int(v), len(names))
        props.append(_make_prop(cr2w, name, t, v))
    return props


def _typed(p, t=None):
    """PROPERTY/ELEMENT -> (name, type, value); structs become [(name, type, value)], arrays plain lists."""
    t = t or p.theType
    name = getattr(p, "theName", t)
    if t.startswith("array:"):
        elem_type = t.split(",")[-1]
        if getattr(p, "More", None):
            v = [_typed(e, elem_type)[2] for e in p.More]
        elif isinstance(getattr(p, "Index", None), list):
            v = [str(getattr(i, "String", i) or "") for i in p.Index]
        else:
            v = list(getattr(p, "value", None) or [])
        return name, t, v
    members = getattr(p, "More", None) or getattr(p, "MoreProps", None)
    if members:
        return name, t, [_typed(e) for e in members]
    if t == "CName":
        return name, t, read_cname_prop(p)
    if t in ("String", "StringAnsi"):
        return name, t, read_string_prop(p)
    if t in Enums.Enum_Types:
        return name, t, read_enum_prop(p)
    return name, t, getattr(p, "Value", 0)


def _make_prop(cr2w, name, t, v):
    if t.startswith("array:"):
        et = t.split(",")[-1]
        return PROPERTY(theName=name, theType=t, elements=[_make_prop(cr2w, et, et, e) for e in v])
    if isinstance(v, list):
        return PROPERTY(theName=name, theType=t, More=[_make_prop(cr2w, n, et, ev) for n, et, ev in v])
    if t == "CName":
        return _make_cname_prop(name, v)
    if t in ("String", "StringAnsi"):
        return _make_string_prop(name, v, theType=t)
    if t in Enums.Enum_Types:
        return _make_enum_prop(cr2w, name, t, v)
    return PROPERTY(Value=v, theName=name, theType=t)


def _remap(triples, index):
    """Re-resolve bone indices from sibling name props: indexFoo <- foo, fooBone <- fooBoneName, fkBones <- fkBonesNames."""
    by_name = {n: v for n, _, v in triples}
    out = []
    for n, t, v in triples:
        if t == "Int32":
            keys = (n[5:6].lower() + n[6:] if n.startswith("index") else "", n + "Name")
            ref = next((k for k in keys if k in by_name), None)
            if ref:
                v = _bone_index(index, by_name[ref], n)
        elif t == "array:2,0,Int32" and n.replace("Bones", "BonesNames") in by_name:
            v = [_bone_index(index, b, n) for b in by_name[n.replace("Bones", "BonesNames")]]
        out.append((n, t, v))
    return out


def _bone_index(index, bone_name, prop_name):
    i = index.get(str(bone_name).lower())
    if i is None:
        log.warning("%s: bone '%s' is not in the exported rig", prop_name, bone_name)
        return -1
    return i
