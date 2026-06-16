from __future__ import annotations

import json
import struct
from typing import Any, Optional

MAGIC = b"W3BUF\x00"
VERSION = 1

_TYPE_FMT = {"f32": ("<f", 4), "u8": ("<B", 1), "u16": ("<H", 2), "u32": ("<I", 4)}


class SubmeshBuffer:
    def __init__(self, lod: int, mat_id: int, material: str):
        self.lod = int(lod)
        self.mat_id = int(mat_id)
        self.material = str(material or "")
        self.vertex_count = 0
        self.index_count = 0
        self.attrs: dict[str, tuple[str, int, Any]] = {}
        self.indices: Any = []

    def set_attr(self, name: str, type_str: str, comps: int, flat_values) -> None:
        if type_str not in _TYPE_FMT:
            raise ValueError(f"Unknown attribute type {type_str!r}")
        self.attrs[name] = (type_str, int(comps), flat_values)

    def set_positions(self, flat_xyz) -> None:
        self.set_attr("position", "f32", 3, flat_xyz)
        self.vertex_count = len(flat_xyz) // 3

    def set_indices(self, flat_indices) -> None:
        self.indices = flat_indices
        self.index_count = len(flat_indices)


class MeshBuffer:
    def __init__(self, mesh_name: str, depot_path: str = "", is_skinned: bool = False):
        self.mesh_name = str(mesh_name or "")
        self.depot_path = str(depot_path or "")
        self.is_skinned = bool(is_skinned)
        self.bone_names: list[str] = []
        self.bone_parents: list[int] = []
        self.bone_poses: list[list[float]] = []
        self.unresolved_bones: list[str] = []
        self.submeshes: list[SubmeshBuffer] = []


def _pack_flat(type_str: str, values) -> bytes:
    fmt_char, _ = _TYPE_FMT[type_str]
    seq = values.tolist() if hasattr(values, "tolist") else list(values)
    return struct.pack(f"<{len(seq)}{fmt_char[1:]}", *seq)


def write_mesh_buffer(path: str, mesh: MeshBuffer) -> dict[str, Any]:
    payload = bytearray()

    def stash(type_str: str, values) -> tuple[int, int]:
        blob = _pack_flat(type_str, values)
        offset = len(payload)
        payload.extend(blob)
        return offset, len(blob)

    submesh_headers: list[dict[str, Any]] = []
    for sm in mesh.submeshes:
        attr_hdr: dict[str, Any] = {}
        for name, (type_str, comps, values) in sm.attrs.items():
            offset, size = stash(type_str, values)
            attr_hdr[name] = {"offset": offset, "size": size, "comps": comps, "type": type_str}
        idx_offset, idx_size = stash("u32", sm.indices)
        submesh_headers.append({
            "lod": sm.lod,
            "mat_id": sm.mat_id,
            "material": sm.material,
            "vertex_count": sm.vertex_count,
            "index_count": sm.index_count,
            "attrs": attr_hdr,
            "indices": {"offset": idx_offset, "size": idx_size, "type": "u32"},
        })

    header: dict[str, Any] = {
        "mesh_name": mesh.mesh_name,
        "depot_path": mesh.depot_path,
        "is_skinned": mesh.is_skinned,
        "submeshes": submesh_headers,
    }
    if mesh.is_skinned:
        header["bones"] = {
            "names": mesh.bone_names,
            "parents": mesh.bone_parents,
            "poses": mesh.bone_poses,
        }

    header_bytes = json.dumps(header, separators=(",", ":")).encode("utf-8")
    with open(path, "wb") as fh:
        fh.write(MAGIC)
        fh.write(struct.pack("<II", VERSION, len(header_bytes)))
        fh.write(header_bytes)
        fh.write(payload)
    return header


def read_mesh_buffer(path: str) -> dict[str, Any]:
    with open(path, "rb") as fh:
        data = fh.read()
    if data[:6] != MAGIC:
        raise ValueError("Not a .w3buf file (bad magic)")
    version, hdr_len = struct.unpack_from("<II", data, 6)
    if version != VERSION:
        raise ValueError(f"Unsupported .w3buf version {version}")
    hdr_start = 6 + 8
    header = json.loads(data[hdr_start:hdr_start + hdr_len].decode("utf-8"))
    payload = memoryview(data)[hdr_start + hdr_len:]

    def unpack(entry: dict[str, Any]) -> list:
        fmt_char, nbytes = _TYPE_FMT[entry["type"]]
        count = entry["size"] // nbytes
        return list(struct.unpack_from(f"<{count}{fmt_char[1:]}", payload, entry["offset"]))

    out_submeshes = []
    for sm in header["submeshes"]:
        attrs = {name: unpack(meta) for name, meta in sm["attrs"].items()}
        out_submeshes.append({
            "lod": sm["lod"],
            "mat_id": sm["mat_id"],
            "material": sm["material"],
            "vertex_count": sm["vertex_count"],
            "index_count": sm["index_count"],
            "attrs": attrs,
            "indices": unpack(sm["indices"]),
        })
    return {"header": header, "submeshes": out_submeshes}
