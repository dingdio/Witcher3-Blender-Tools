"""Export a Blender armature as a Witcher 3 .w2rig (native) or .w2rig.json (legacy WolvenKit)."""
import copy
import json
import logging
import math
import os

from mathutils import Matrix

from .. import file_helpers, get_rig_rot90_enabled
from ..CR2W import cr2w_writer, rig_builder
from ..CR2W.common_blender import repo_file, win_safe_path
from ..CR2W.dc_skeleton import getCR2W, open_cr2w_read_stream
from ..CR2W.json_convert.CR2WJsonObject import CR2WJsonScalar, getRigTemplate
from ..importers.import_rig import get_ordered_bones
from .export_anims import get_selected_armature

log = logging.getLogger(__name__)
_ROT90_UNDO = Matrix.Rotation(math.radians(90), 4, 'Z')


def _parents_first(bones):
    out, seen = [], set()

    def visit(bone):
        if bone.name in seen:
            return
        if bone.parent:
            visit(bone.parent)
        seen.add(bone.name)
        out.append(bone)

    for bone in bones:
        visit(bone)
    return out


def collect_rig_spec(armature):
    """Engine-space rest pose per bone; imported bone order kept, new bones appended."""
    rig_settings = armature.data.witcherui_RigSettings
    undo = _ROT90_UNDO if get_rig_rot90_enabled(rig_settings) else Matrix.Identity(4)
    bones = _parents_first(get_ordered_bones(armature))
    index = {b.name: i for i, b in enumerate(bones)}
    rest = {b.name: b.matrix_local @ undo for b in bones}
    spec = []
    for b in bones:
        local = rest[b.parent.name].inverted() @ rest[b.name] if b.parent else rest[b.name]
        pos, rot, _ = local.decompose()
        spec.append({
            "name": file_helpers.rm_ns(b.name),
            "parent": index[b.parent.name] if b.parent else -1,
            "pos": tuple(pos),
            "rot": (rot.x, rot.y, rot.z, rot.w),
            "scale": (1.0, 1.0, 1.0),
        })
    tracks = [t.name for t in rig_settings.witcher_tracks_list]
    return spec, tracks


def _load_source_rig(path):
    path = str(path or "").strip()
    if not path:
        return None
    if not os.path.isfile(win_safe_path(path)):
        path = repo_file(path)
    try:
        cr2w = getCR2W(open_cr2w_read_stream(path))
    except Exception:
        log.warning("Source rig %s not readable; exporting without engine metadata", path, exc_info=True)
        return None
    return cr2w if cr2w.HEADER.version > 115 else None


def _write_json(filepath, bones):
    rig = getRigTemplate()
    skelly = rig._chunks['CSkeleton #0']
    bone_template = skelly._vars['bones']._elements[0]
    rigdata_template = skelly._vars['rigdata']._elements[0]
    bones_json, rigdata_json = [], []
    for b in bones:
        bone = copy.deepcopy(bone_template)
        bone._vars['name'] = CR2WJsonScalar(_type='StringAnsi', _value=b["name"])
        bone._vars['nameAsCName'] = CR2WJsonScalar(_type='CName', _value=b["name"])
        bones_json.append(bone)
        rigdata = copy.deepcopy(rigdata_template)
        for key, values in (('Position', b["pos"]), ('Rotation', b["rot"]), ('Scale', b["scale"])):
            for axis, value in zip('XYZW', values):
                rigdata._vars[key]._vars[axis] = CR2WJsonScalar(_type='Float', _value=float(value))
        rigdata_json.append(rigdata)
    skelly._vars['bones']._elements = bones_json
    skelly._vars['parentIndices']._elements = [CR2WJsonScalar(_type='Int16', _value=b["parent"]) for b in bones]
    skelly._vars['rigdata']._elements = rigdata_json
    with open(win_safe_path(filepath), "w") as f:
        f.write(json.dumps(rig, default=vars, sort_keys=False, separators=(',', ':')))


def export_w3_rig(context, filepath, use_json_legacy=False):
    armature = get_selected_armature(context)
    if armature is None:
        log.warning("No armature selected for rig export")
        return {'CANCELLED'}
    bones, tracks = collect_rig_spec(armature)
    if use_json_legacy:
        _write_json(filepath, bones)
    else:
        source = _load_source_rig(armature.data.witcherui_RigSettings.main_entity_skeleton)
        cr2w_writer.write_w2rig(rig_builder.build_w2rig(bones, tracks, source_cr2w=source), filepath)
    log.info("Exported rig %s (%d bones)", filepath, len(bones))
    return {'FINISHED'}
