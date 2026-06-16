from __future__ import annotations

from typing import Any, Optional

from . import mesh_buffer


def extract_armature_skeleton(armature) -> dict:
    import mathutils

    T = mathutils.Matrix.Diagonal((1.0, -1.0, 1.0, 1.0))
    Tinv = T.inverted()

    ordered: list = []
    seen: set = set()

    def add(bone):
        if bone.name in seen:
            return
        if bone.parent is not None:
            add(bone.parent)
        seen.add(bone.name)
        ordered.append(bone)

    for bone in armature.data.bones:
        add(bone)

    index = {bone.name: i for i, bone in enumerate(ordered)}
    names, parents, poses = [], [], []
    for bone in ordered:
        names.append(bone.name)
        parents.append(index[bone.parent.name] if bone.parent else -1)
        local = (bone.parent.matrix_local.inverted() @ bone.matrix_local) if bone.parent else bone.matrix_local
        local_ue = T @ local @ Tinv
        loc, quat, scale = local_ue.decompose()  # quat is (w, x, y, z)
        if bone.parent is None:
            scale *= 100.0
        poses.append([loc.x, loc.y, loc.z, quat.x, quat.y, quat.z, quat.w, scale.x, scale.y, scale.z])

    return {"names": names, "parents": parents, "poses": poses, "index": index}


def _skinning_arrays(meshDataBl, max_influences: int = 4):
    vert_count = len(meshDataBl.vertex3DCoords)
    by_vertex: dict[int, list[tuple[int, float]]] = {}
    for entry in meshDataBl.skinningVerts or []:
        vid = int(entry.vertexId)
        by_vertex.setdefault(vid, []).append((int(entry.boneId), float(entry.strength)))

    bone_index: list[int] = []
    bone_weight: list[float] = []
    for vid in range(vert_count):
        influences = sorted(by_vertex.get(vid, []), key=lambda iw: iw[1], reverse=True)[:max_influences]
        total = sum(w for _, w in influences) or 1.0
        idx4 = [0, 0, 0, 0]
        wgt4 = [0.0, 0.0, 0.0, 0.0]
        for slot, (bone, weight) in enumerate(influences):
            idx4[slot] = max(0, min(65535, bone))
            wgt4[slot] = weight / total
        bone_index.extend(idx4)
        bone_weight.extend(wgt4)
    return bone_index, bone_weight


def _has_meaningful(rows, default_tuple) -> bool:
    eps = 1e-6
    for row in rows or []:
        for value, default in zip(row, default_tuple):
            if abs(float(value) - default) > eps:
                return True
    return False


def _submesh_from_meshdata(meshDataBl, material_name: str) -> Optional[mesh_buffer.SubmeshBuffer]:
    info = meshDataBl.meshInfo
    positions = meshDataBl.vertex3DCoords
    vert_count = len(positions)
    if vert_count == 0 or not meshDataBl.faces:
        return None

    sm = mesh_buffer.SubmeshBuffer(
        lod=getattr(info, "lod", 0) or 0,
        mat_id=getattr(info, "materialID", 0) or 0,
        material=material_name,
    )
    sm.set_positions([float(c) for xyz in positions for c in xyz[:3]])

    normals = list(meshDataBl.normalsAll or [])
    if len(normals) == vert_count * 3:
        sm.set_attr("normal", "f32", 3, [float(v) for v in normals])
    elif meshDataBl.normals and len(meshDataBl.normals) == vert_count:
        sm.set_attr("normal", "f32", 3, [float(c) for n in meshDataBl.normals for c in n[:3]])

    tangents = meshDataBl.tangent_vector or []
    if len(tangents) == vert_count:
        comps = 4 if len(tangents[0]) >= 4 else 3
        flat = [float(c) for t in tangents for c in (list(t)[:comps] if comps == 4 else (list(t)[:3]))]
        sm.set_attr("tangent", "f32", comps, flat)

    uv0 = meshDataBl.UV_vertex3DCoords
    if len(uv0) == vert_count:
        sm.set_attr("uv0", "f32", 2, [float(c) for uv in uv0 for c in uv[:2]])
    uv1 = meshDataBl.UV2_vertex3DCoords
    if len(uv1) == vert_count and _has_meaningful(uv1, (0.0, 1.0)):
        sm.set_attr("uv1", "f32", 2, [float(c) for uv in uv1 for c in uv[:2]])

    colors = meshDataBl.vertexColor
    if colors and len(colors) == vert_count and _has_meaningful(colors, (0.0, 0.0, 0.0, 0.0)):
        sm.set_attr("color", "u8", 4,
                    [max(0, min(255, int(round(float(c) * 255.0)))) for col in colors for c in (list(col) + [0, 0, 0, 0])[:4]])

    if meshDataBl.skinningVerts:
        bone_index, bone_weight = _skinning_arrays(meshDataBl)
        sm.set_attr("bone_index", "u16", 4, bone_index)
        sm.set_attr("bone_weight", "f32", 4, bone_weight)

    sm.set_indices([int(i) for face in meshDataBl.faces for i in face[:3]])
    return sm


def _identity_pose() -> list[float]:
    return [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0]


def _apply_skeleton(mesh: mesh_buffer.MeshBuffer, joint_names: list[str],
                    export_skeleton: Optional[dict]) -> list[str]:
    if export_skeleton is None:
        mesh.bone_names = joint_names
        mesh.bone_parents = [-1] * len(joint_names)
        mesh.bone_poses = [_identity_pose() for _ in joint_names]
        return []

    mesh.bone_names = export_skeleton["names"]
    mesh.bone_parents = export_skeleton["parents"]
    mesh.bone_poses = export_skeleton["poses"]
    skel_index = export_skeleton.get("index") or {n: i for i, n in enumerate(mesh.bone_names)}
    remap = [skel_index.get(name, 0) for name in joint_names]
    unresolved = sorted({name for name in joint_names if name not in skel_index})

    for sm in mesh.submeshes:
        entry = sm.attrs.get("bone_index")
        if not entry:
            continue
        type_str, comps, values = entry
        sm.attrs["bone_index"] = (
            type_str, comps,
            [remap[v] if v < len(remap) else 0 for v in values],
        )
    return unresolved


def gather_mesh(filename: str, *, keep_lod_meshes: bool = False,
                embedded_cmesh_chunk_index: Optional[int] = None,
                export_skeleton: Optional[dict] = None) -> mesh_buffer.MeshBuffer:
    from ..CR2W import dc_mesh
    from ..importers.import_mesh import _sanitize_mesh_faces_for_import
    try:
        from ..importers.import_mesh import get_repo_from_abs_path
    except Exception:  # pragma: no cover - depot resolution is best-effort
        get_repo_from_abs_path = lambda p: ""  # noqa: E731

    CData, bufferInfos, material_names, materials, meshName, meshFile = dc_mesh.load_bin_mesh(
        filename, keep_lod_meshes=keep_lod_meshes, embedded_cmesh_chunk_index=embedded_cmesh_chunk_index
    )

    depot_path = ""
    try:
        depot_path = get_repo_from_abs_path(getattr(meshFile, "fileName", "") or filename) or ""
    except Exception:
        depot_path = ""

    is_skinned = any(
        getattr(getattr(m, "meshInfo", None), "vertexType", None) is not None
        and getattr(m.meshInfo, "vertexType").name == "EMVT_SKINNED"
        for m in getattr(CData, "meshDataAllMeshes", []) or []
        if getattr(getattr(m, "meshInfo", None), "vertexType", None) is not None
    )

    mesh = mesh_buffer.MeshBuffer(meshName, depot_path=depot_path, is_skinned=is_skinned)

    for idx, meshDataBl in enumerate(getattr(CData, "meshDataAllMeshes", []) or []):
        _sanitize_mesh_faces_for_import(meshDataBl)
        mat_id = getattr(getattr(meshDataBl, "meshInfo", None), "materialID", 0) or 0
        mat_name = material_names[mat_id] if material_names and 0 <= mat_id < len(material_names) else ""
        sm = _submesh_from_meshdata(meshDataBl, str(mat_name or ""))
        if sm is not None:
            mesh.submeshes.append(sm)

    if is_skinned:
        joint_names = [str(n) for n in (CData.boneData.jointNames or [])]
        mesh.unresolved_bones = _apply_skeleton(mesh, joint_names, export_skeleton)

    return mesh


def gather_placement_mesh(filename: str, *, version: Optional[int] = None,
                          warnings: Optional[list] = None):
    from ..CR2W import dc_mesh
    from ..importers.import_mesh import _sanitize_mesh_faces_for_import
    try:
        from ..importers.import_mesh import get_repo_from_abs_path
    except Exception:  # pragma: no cover - depot resolution is best-effort
        get_repo_from_abs_path = lambda p: ""  # noqa: E731
    from . import mesh_materials

    warnings = warnings if warnings is not None else []

    CData, _bufferInfos, material_names, materials, meshName, meshFile = dc_mesh.load_bin_mesh(
        filename, keep_lod_meshes=False
    )
    if version is None:
        version = getattr(getattr(meshFile, "HEADER", None), "version", 999)

    depot_path = ""
    try:
        depot_path = get_repo_from_abs_path(getattr(meshFile, "fileName", "") or filename) or ""
    except Exception:
        depot_path = ""

    mesh = mesh_buffer.MeshBuffer(meshName, depot_path=depot_path, is_skinned=False)
    for meshDataBl in getattr(CData, "meshDataAllMeshes", []) or []:
        _sanitize_mesh_faces_for_import(meshDataBl)
        mat_id = getattr(getattr(meshDataBl, "meshInfo", None), "materialID", 0) or 0
        mat_name = material_names[mat_id] if material_names and 0 <= mat_id < len(material_names) else ""
        sm = _submesh_from_meshdata(meshDataBl, str(mat_name or ""))
        if sm is not None:
            sm.attrs.pop("bone_index", None)
            sm.attrs.pop("bone_weight", None)
            mesh.submeshes.append(sm)

    slots = mesh_materials.material_slots_from_mesh(material_names, materials, meshFile, version, warnings)
    return mesh, slots
