import logging
import os
import json
import struct
import re
from array import array
from math import radians
import bmesh
import bpy

log = logging.getLogger(__name__)
from pathlib import Path
from bpy.types import (Panel, Operator)
from bpy.props import StringProperty, BoolProperty, FloatProperty
from mathutils import Vector
from bpy_extras.io_utils import ImportHelper, ExportHelper
import addon_utils
from .. import file_helpers, w3_material_blender, CR2W, get_texture_path, get_uncook_path
from ..cloth_util import setup_w3_material_CR2W
from ..CR2W.common_blender import bpy_image_load_safe, win_safe_path
from ..CR2W.witcher_cache.TextureCache.DDSUtils import DDSUtils
from ..CR2W.witcher_cache.TextureCache.DDS_Metadata import DDSMetadata
from ..CR2W.witcher_cache.TextureCache.DDS_Enums import EFormat

from ..CR2W.CR2W_types import getCR2W
from ..CR2W import bStream
from ..ui.blender_fun import convert_xbm_to_dds, load_w2cube_image


_CUBEMAP_FACE_KEYS = ("PX", "NX", "PY", "NY", "PZ", "NZ")
_CUBEMAP_FACE_LABELS = {
    "PX": "+X",
    "NX": "-X",
    "PY": "+Y",
    "NY": "-Y",
    "PZ": "+Z",
    "NZ": "-Z",
}
_CUBEMAP_FACE_W2_SUFFIX = {
    "PY": "fr",  # front
    "NY": "bk",  # back
    "PZ": "up",  # top
    "NZ": "dn",  # bottom
    "PX": "rt",  # right
    "NX": "lf",  # left
}
_CUBEMAP_FACE_MAPPING_ROT_DEG = {
    "PY": (180.0, 0.0, 0.0),    # __fr
    "NY": (0.0, 180.0, 0.0),    # __bk
    "PZ": (180.0, 0.0, 0.0),    # __up
    "NZ": (180.0, 0.0, 180.0),  # __dn
    "PX": (0.0, 180.0, 90.0),   # __rt
    "NX": (180.0, 0.0, 90.0),      # __lf
}
_W2CUBE_FACE_SLOT_NAMES = ("front", "back", "top", "bottom", "left", "right")
_UNCOOKED_W2CUBE_DEFAULT_FACE_SLOT_ORDER = ("front", "back", "top", "bottom", "right", "left")
_UNCOOKED_W2CUBE_SLOT_TO_CUBEMAP_FACE = {
    "right": "PX",
    "left": "NX",
    "front": "PY",
    "back": "NY",
    "top": "PZ",
    "bottom": "NZ",
}


def _dev_get_link_collection(context):
    collection = getattr(context, "collection", None)
    if collection:
        return collection

    view_layer = getattr(context, "view_layer", None)
    active_layer_collection = getattr(view_layer, "active_layer_collection", None) if view_layer else None
    if active_layer_collection and getattr(active_layer_collection, "collection", None):
        return active_layer_collection.collection

    scene = getattr(context, "scene", None)
    if scene and getattr(scene, "collection", None):
        return scene.collection
    return None


def _json_scalar_value(value):
    if isinstance(value, dict) and "_value" in value and len(value) <= 2:
        return value.get("_value")
    return value


def _cr2w_json_node_to_typed_python(node):
    """Convert CR2WJson* objects to plain Python while preserving `_type/_value` wrappers."""
    if node is None:
        return None

    if hasattr(node, "_value"):
        return {
            "_type": getattr(node, "_type", None),
            "_value": getattr(node, "_value", None),
        }

    if hasattr(node, "_elements"):
        out = {
            "_type": getattr(node, "_type", None),
            "_elements": [],
        }
        if getattr(node, "_bufferPadding", None) is not None:
            out["_bufferPadding"] = node._bufferPadding
        for el in getattr(node, "_elements", []) or []:
            out["_elements"].append(_cr2w_json_node_to_typed_python(el))
        return out

    if hasattr(node, "_vars"):
        out = {
            "_type": getattr(node, "_type", None),
            "_vars": {},
        }
        for key, val in (getattr(node, "_vars", {}) or {}).items():
            out["_vars"][key] = _cr2w_json_node_to_typed_python(val)
        for extra_attr in ("_key", "_parentKey", "_flags", "_unknownBytes"):
            if hasattr(node, extra_attr):
                out[extra_attr] = getattr(node, extra_attr)
        return out

    if isinstance(node, dict):
        return {k: _cr2w_json_node_to_typed_python(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_cr2w_json_node_to_typed_python(v) for v in node]
    return node


def _typed_to_plain_values(node):
    """Strip `_type/_vars/_elements/_value` wrappers to plain dict/list/scalars."""
    if isinstance(node, dict):
        if "_value" in node and set(node.keys()).issubset({"_type", "_value"}):
            return node.get("_value")
        if "_vars" in node and "_type" in node:
            vars_map = node.get("_vars") or {}
            return {k: _typed_to_plain_values(v) for k, v in vars_map.items()}
        if "_elements" in node and "_type" in node:
            return [_typed_to_plain_values(v) for v in (node.get("_elements") or [])]
        return {k: _typed_to_plain_values(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_typed_to_plain_values(v) for v in node]
    return node


def _sanitize_metadata_for_text(node, *, max_string=512, max_list=64, _depth=0):
    """Keep metadata readable in Blender Text Editor; truncate giant binary/base64 payloads."""
    if _depth > 20:
        return "<max-depth>"

    if isinstance(node, str):
        if len(node) > max_string:
            return {
                "_truncated": True,
                "_len": len(node),
                "_head": node[:160],
                "_tail": node[-80:],
            }
        return node

    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if isinstance(v, str) and k.lower() in {"image"} and len(v) > 256:
                out[k] = {"_truncated": True, "_len": len(v), "_kind": "image-bytes/base64"}
            else:
                out[k] = _sanitize_metadata_for_text(v, max_string=max_string, max_list=max_list, _depth=_depth + 1)
        return out

    if isinstance(node, list):
        if len(node) > max_list:
            return {
                "_truncated": True,
                "_len": len(node),
                "_head": [_sanitize_metadata_for_text(v, max_string=max_string, max_list=max_list, _depth=_depth + 1)
                          for v in node[:8]],
            }
        return [_sanitize_metadata_for_text(v, max_string=max_string, max_list=max_list, _depth=_depth + 1) for v in node]

    return node


def _find_ci_key(dct, key_name: str):
    if not isinstance(dct, dict):
        return None
    key_name = (key_name or "").lower()
    for key in dct.keys():
        if str(key).lower() == key_name:
            return key
    return None


def _coerce_bool(val):
    val = _json_scalar_value(val)
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        low = val.strip().lower()
        if low in {"true", "1", "yes"}:
            return True
        if low in {"false", "0", "no"}:
            return False
    return val


def _flatten_face_metadata(face_obj):
    """Extract the key cube-face fields from a nested parsed face object."""
    result = {}

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                lk = str(k).lower()
                if lk in {"m_texture", "sourcetexture", "m_rotate", "m_flipx", "m_flipy"}:
                    out_key = {
                        "m_texture": "m_texture",
                        "sourcetexture": "sourceTexture",
                        "m_rotate": "m_rotate",
                        "m_flipx": "m_flipX",
                        "m_flipy": "m_flipY",
                    }[lk]
                    val = _typed_to_plain_values(v)
                    if out_key in {"m_rotate", "m_flipX", "m_flipY"}:
                        val = _coerce_bool(val)
                    result[out_key] = val
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(face_obj)
    return result


def _read_w2cube_chunk_metadata(path: str):
    """Read CCubeTexture metadata from cooked or uncooked .w2cube using CR2W parser."""
    meta = {
        "source_path": path,
        "exists": os.path.exists(path),
        "ccube_texture": None,
        "cube_faces": {},
        "has_face_metadata": False,
        "is_uncooked_source": False,
        "has_runtime_payload": False,
        "errors": [],
    }
    if not meta["exists"]:
        meta["errors"].append("file_not_found")
        return meta

    try:
        with open(path, "rb") as f:
            cr2w = getCR2W(f)
    except Exception as e:
        meta["errors"].append(f"parse_failed: {e}")
        return meta

    try:
        chunk = next((c for c in getattr(getattr(cr2w, "CHUNKS", None), "CHUNKS", []) if getattr(c, "Type", "") == "CCubeTexture"), None)
    except Exception as e:
        meta["errors"].append(f"chunk_scan_failed: {e}")
        chunk = None

    if not chunk:
        meta["errors"].append("no_ccubetexture_chunk")
        return meta

    typed_vars = {}
    plain_vars = {}
    try:
        for prop in getattr(chunk, "PROPS", []) or []:
            prop_name = getattr(prop, "theName", None) or ""
            if not prop_name:
                continue
            node = cr2w.WalkNode(prop)
            typed = _cr2w_json_node_to_typed_python(node)
            typed_vars[prop_name] = typed
            plain_vars[prop_name] = _typed_to_plain_values(typed)
    except Exception as e:
        meta["errors"].append(f"walknode_failed: {e}")

    runtime_fields = {}
    for key in ("Texturecachekey", "Residentmip", "Encodedformat", "Edge", "Mipmapscount", "Filesize", "Ffffffff"):
        real_key = _find_ci_key(plain_vars, key)
        if real_key is not None:
            runtime_fields[key] = plain_vars.get(real_key)
    if runtime_fields:
        meta["has_runtime_payload"] = True

    face_meta = {}
    for slot in _W2CUBE_FACE_SLOT_NAMES:
        real_key = _find_ci_key(plain_vars, slot)
        if real_key is None:
            continue
        raw_face = plain_vars.get(real_key)
        flattened = _flatten_face_metadata(raw_face)
        face_entry = {
            "slot": slot,
            "raw": raw_face,
        }
        if flattened:
            face_entry.update(flattened)
        face_meta[slot] = face_entry

    has_face_metadata = any(
        any(k in face for k in ("m_rotate", "m_flipX", "m_flipY", "m_texture", "sourceTexture"))
        for face in face_meta.values()
    )

    # Keep typed vars for exact inspection, but sanitize huge payload strings.
    ccube = {
        "chunk_type": "CCubeTexture",
        "vars_typed": _sanitize_metadata_for_text(typed_vars),
        "vars_plain": _sanitize_metadata_for_text(plain_vars),
        "runtime_fields": runtime_fields,
    }
    meta["ccube_texture"] = ccube
    meta["cube_faces"] = _sanitize_metadata_for_text(face_meta)
    meta["has_face_metadata"] = bool(has_face_metadata)
    meta["is_uncooked_source"] = bool(has_face_metadata)
    return meta


def _pair_cooked_from_uncooked_w2cube(path: str):
    low = path.lower()
    if low.endswith("_uncooked.w2cube"):
        candidate = path[:-len("_uncooked.w2cube")] + ".w2cube"
        if os.path.exists(candidate):
            return candidate
    return None


def _pair_uncooked_from_cooked_w2cube(path: str):
    low = path.lower()
    if low.endswith(".w2cube") and not low.endswith("_uncooked.w2cube"):
        candidate = path[:-len(".w2cube")] + "_uncooked.w2cube"
        if os.path.exists(candidate):
            return candidate
    return None


def _write_w2cube_metadata_text(meta: dict, name_hint: str = "w2cube_meta"):
    """Write/update a Blender Text datablock with parsed w2cube metadata."""
    if not meta:
        return None
    safe_hint = "".join(ch if (ch.isalnum() or ch in "_-.") else "_" for ch in (name_hint or "w2cube_meta"))
    text_name = f"{safe_hint}.json"
    txt = bpy.data.texts.get(text_name)
    if txt is None:
        txt = bpy.data.texts.new(text_name)
    txt.clear()
    txt.write(json.dumps(meta, indent=2, ensure_ascii=False))
    return text_name


def _attach_w2cube_metadata_to_object(obj, meta: dict, text_name: str = ""):
    if not obj or not meta:
        return
    try:
        obj["witcher_w2cube_meta_path"] = str(meta.get("source_path") or "")
        obj["witcher_w2cube_meta_has_faces"] = bool(meta.get("has_face_metadata"))
        obj["witcher_w2cube_meta_is_uncooked"] = bool(meta.get("is_uncooked_source"))
        if text_name:
            obj["witcher_w2cube_meta_text"] = text_name
        faces = meta.get("cube_faces") or {}
        for slot in _W2CUBE_FACE_SLOT_NAMES:
            face = faces.get(slot) or {}
            for key in ("m_rotate", "m_flipX", "m_flipY"):
                if key in face:
                    obj[f"w2cube_{slot}_{key}"] = bool(face.get(key))
            if "m_texture" in face and isinstance(face.get("m_texture"), str):
                obj[f"w2cube_{slot}_texture"] = face.get("m_texture")
    except Exception:
        pass


def _rgba8_full_mip_chain_face_size(edge: int) -> int:
    edge = int(edge or 0)
    if edge <= 0:
        return 0
    total = 0
    w = edge
    h = edge
    while True:
        total += max(1, w) * max(1, h) * 4
        if w == 1 and h == 1:
            break
        w = max(1, w // 2)
        h = max(1, h // 2)
    return total


def _block_compressed_full_mip_chain_face_size(edge: int, block_bytes: int) -> int:
    edge = int(edge or 0)
    if edge <= 0:
        return 0
    total = 0
    w = edge
    h = edge
    while True:
        bw = max(1, (w + 3) // 4)
        bh = max(1, (h + 3) // 4)
        total += bw * bh * int(block_bytes)
        if w == 1 and h == 1:
            break
        w = max(1, w // 2)
        h = max(1, h // 2)
    return total


def _find_uncooked_w2cube_rgba8_payload_layout(raw: bytes):
    """Detect a raw 6-face RGBA8 full-mip cubemap payload appended to an uncooked .w2cube.

    Returns `(edge, mip_count, payload_start, face_full_size)` or `None`.
    """
    if not raw:
        return None

    # Heuristic: uncooked authoring files have a small CR2W header and then the
    # raw cubemap payload packed at the end of the file. We choose the largest
    # power-of-two face size that leaves a plausible header size.
    file_size = len(raw)
    candidates = []
    for exp in range(3, 14):  # 8 .. 8192
        edge = 1 << exp
        face_full_size = _rgba8_full_mip_chain_face_size(edge)
        payload_size = face_full_size * 6
        if payload_size <= 0 or payload_size > file_size:
            continue
        header_size = file_size - payload_size
        if not (16 <= header_size <= 65536):
            continue
        mip_count = exp + 1
        candidates.append((edge, mip_count, file_size - payload_size, face_full_size))

    if not candidates:
        return None

    # Prefer the largest valid edge; smaller edges imply an implausibly huge header.
    return max(candidates, key=lambda c: c[0])


def _is_probable_uncooked_w2cube_raw_tail_file(path: str) -> bool:
    """Fast content-based probe so uncooked files don't go through slow CR2W parsing."""
    try:
        raw = Path(path).read_bytes()
    except Exception:
        return False
    # Try several plausible raw-tail payload layouts used by uncooked cubemaps:
    # uncompressed RGBA8, BC1, BC3/BC7.
    candidates = []
    file_size = len(raw)
    for exp in range(3, 14):  # 8..8192
        edge = 1 << exp
        for face_full_size in (
            _rgba8_full_mip_chain_face_size(edge),
            _block_compressed_full_mip_chain_face_size(edge, 8),
            _block_compressed_full_mip_chain_face_size(edge, 16),
        ):
            payload_size = int(face_full_size) * 6
            if payload_size <= 0 or payload_size > file_size:
                continue
            header_size = file_size - payload_size
            if not (16 <= header_size <= 65536):
                continue
            candidates.append((edge, file_size - payload_size))
    if not candidates:
        return False
    # Prefer the largest edge candidate and inspect its header.
    edge, payload_start = max(candidates, key=lambda c: c[0])
    _ = edge  # debug/readability only
    header = raw[:min(max(int(payload_start), 0), 4096)]
    return (b"CCubeTexture" in header) and ((b"CBitmapTexture" in header) or (b"CubeFace" in header))


def _extract_uncooked_w2cube_face_paths(header_bytes: bytes):
    """Best-effort parse of face XBM paths from the uncooked header region."""
    if not header_bytes:
        return [], {}, []

    seen = set()
    ordered_paths = []
    for match in re.finditer(rb'<([^\x00\r\n]{1,512}?\.xbm)', header_bytes):
        try:
            path = match.group(1).decode("latin-1")
        except Exception:
            continue
        low = path.lower()
        if ".xbm" not in low:
            continue
        if low in seen:
            continue
        seen.add(low)
        ordered_paths.append(path)

    code_to_slot = {
        "fr": "front",
        "bk": "back",
        "up": "top",
        "dn": "bottom",
        "rt": "right",
        "lf": "left",
    }
    face_paths_by_slot = {}
    ordered_slots = []
    for path in ordered_paths:
        base = os.path.basename(path).lower()
        slot = None
        m = re.search(r'(?:^|_)(fr|bk|up|dn|rt|lf)\.xbm$', base)
        if m:
            slot = code_to_slot.get(m.group(1))
        elif "front" in base:
            slot = "front"
        elif "back" in base:
            slot = "back"
        elif "top" in base or "up" in base:
            slot = "top"
        elif "bottom" in base or "down" in base or "dn" in base:
            slot = "bottom"
        elif "right" in base or "rt" in base:
            slot = "right"
        elif "left" in base or "lf" in base:
            slot = "left"
        if slot:
            face_paths_by_slot[slot] = path
            ordered_slots.append(slot)

    return ordered_paths, face_paths_by_slot, ordered_slots


def _guess_alpha_channel_index_rgba8(raw_faces_top):
    """Return the most likely alpha byte offset (0..3) for opaque-ish RGBA8 data."""
    scores = [0, 0, 0, 0]
    samples = 0
    for face in raw_faces_top:
        if not face:
            continue
        # Sample every 32nd pixel to keep this cheap.
        step = 32 * 4
        for off in range(0, len(face), step):
            if off + 3 >= len(face):
                break
            px = face[off:off + 4]
            samples += 1
            for i in range(4):
                v = px[i]
                if v >= 250:
                    scores[i] += 2
                elif v <= 5:
                    scores[i] += 0  # alpha can be nonzero; don't reward zeros
    if samples <= 0:
        return 3
    return max(range(4), key=lambda idx: scores[idx])


def _channel_labels_from_alpha_index(alpha_index: int) -> str:
    """Return input byte labels string (e.g. 'RGBA', 'BGRA', 'ARGB')."""
    # Most observed uncooked cubemaps in this toolset appear to store alpha in
    # the last byte and color channels in RGB order (sample bytes look blue-ish).
    if alpha_index == 3:
        return "RGBA"
    if alpha_index == 0:
        return "ARGB"
    if alpha_index == 1:
        return "RAGB"
    if alpha_index == 2:
        return "RGAB"
    return "RGBA"


def _reorder_raw_4byte_pixels_to_rgba(raw_bytes: bytes, input_labels: str) -> bytes:
    labels = (input_labels or "RGBA").upper()
    if len(labels) != 4 or any(ch not in "RGBA" for ch in labels):
        labels = "RGBA"
    idx_r = labels.index("R")
    idx_g = labels.index("G")
    idx_b = labels.index("B")
    idx_a = labels.index("A")
    if (idx_r, idx_g, idx_b, idx_a) == (0, 1, 2, 3):
        return raw_bytes
    src = memoryview(raw_bytes)
    out = bytearray(len(raw_bytes))
    for i in range(0, len(raw_bytes), 4):
        out[i + 0] = src[i + idx_r]
        out[i + 1] = src[i + idx_g]
        out[i + 2] = src[i + idx_b]
        out[i + 3] = src[i + idx_a]
    return bytes(out)


def _flip_rgba8_image_rows(raw_bytes: bytes, width: int, height: int) -> bytes:
    """Flip rows vertically for Blender image pixel upload (bottom-left origin)."""
    width = int(width or 0)
    height = int(height or 0)
    if width <= 0 or height <= 0:
        return raw_bytes
    row_size = width * 4
    if len(raw_bytes) != row_size * height:
        return raw_bytes
    out = bytearray(len(raw_bytes))
    for y in range(height):
        src_off = (height - 1 - y) * row_size
        dst_off = y * row_size
        out[dst_off:dst_off + row_size] = raw_bytes[src_off:src_off + row_size]
    return bytes(out)


def _create_or_update_image_from_rgba8(name: str, width: int, height: int, rgba_bytes: bytes, *, colorspace='sRGB'):
    if width <= 0 or height <= 0:
        return None
    if len(rgba_bytes) != width * height * 4:
        return None

    img = bpy.data.images.get(name)
    if img is not None:
        try:
            if int(getattr(img, "size", [0, 0])[0]) != width or int(getattr(img, "size", [0, 0])[1]) != height:
                bpy.data.images.remove(img)
                img = None
        except Exception:
            img = None

    if img is None:
        img = bpy.data.images.new(name=name, width=width, height=height, alpha=True)

    try:
        img.alpha_mode = 'STRAIGHT'
    except Exception:
        pass
    try:
        img.colorspace_settings.name = colorspace
    except Exception:
        pass

    floats = array('f', (b / 255.0 for b in rgba_bytes))
    img.pixels.foreach_set(floats)
    try:
        img.update()
    except Exception:
        pass
    return img


def _read_uncooked_w2cube_raw_faces(path: str):
    """Read a raw-payload uncooked .w2cube and build face images for cube preview.

    This bypasses the CR2W parser and uses the observed layout:
      [CR2W header/metadata][6 x RGBA8 cubemap faces with full mip chains]
    """
    raw = Path(path).read_bytes()
    if b"CCubeTexture" not in raw[:4096]:
        raise ValueError("Not an uncooked CCubeTexture source (CCubeTexture marker not found near file start)")
    if b"CBitmapTexture" not in raw[:4096]:
        raise ValueError("Uncooked cubemap does not contain expected CBitmapTexture markers")

    layout = _find_uncooked_w2cube_rgba8_payload_layout(raw)
    if not layout:
        raise ValueError("Could not detect uncooked RGBA8 cubemap payload layout")
    edge, mip_count, payload_start, face_full_size = layout
    payload = raw[payload_start:]
    if len(payload) != face_full_size * 6:
        raise ValueError("Detected uncooked payload size mismatch")

    top_size = edge * edge * 4
    raw_faces_top = []
    for i in range(6):
        face_chunk = payload[i * face_full_size:(i + 1) * face_full_size]
        top = face_chunk[:top_size]
        if len(top) != top_size:
            raise ValueError(f"Face {i} top mip size mismatch")
        raw_faces_top.append(top)

    header_bytes = raw[:payload_start]
    ordered_paths, face_paths_by_slot, ordered_slots = _extract_uncooked_w2cube_face_paths(header_bytes)
    slot_order = []
    for slot in ordered_slots:
        if slot not in slot_order:
            slot_order.append(slot)
    for slot in _UNCOOKED_W2CUBE_DEFAULT_FACE_SLOT_ORDER:
        if slot not in slot_order:
            slot_order.append(slot)
    slot_order = tuple(slot_order[:6])

    alpha_index = _guess_alpha_channel_index_rgba8(raw_faces_top)
    input_labels = _channel_labels_from_alpha_index(alpha_index)

    stem = Path(path).stem or "w2cube_uncooked"
    face_images = {}
    cube_faces_meta = {}
    payload_faces = []

    for face_index, top_bytes in enumerate(raw_faces_top):
        slot = slot_order[face_index] if face_index < len(slot_order) else _UNCOOKED_W2CUBE_DEFAULT_FACE_SLOT_ORDER[face_index]
        cubemap_face_key = _UNCOOKED_W2CUBE_SLOT_TO_CUBEMAP_FACE.get(slot)
        if not cubemap_face_key:
            continue

        rgba = _reorder_raw_4byte_pixels_to_rgba(top_bytes, input_labels=input_labels)
        rgba = _flip_rgba8_image_rows(rgba, edge, edge)

        img_name = f"{stem}_uncooked_{cubemap_face_key.lower()}"
        img = _create_or_update_image_from_rgba8(img_name, edge, edge, rgba, colorspace='sRGB')
        if img is None:
            raise ValueError(f"Failed to create Blender image for face {face_index} ({slot})")

        face_images[cubemap_face_key] = img
        payload_faces.append({
            "payload_index": face_index,
            "slot": slot,
            "cubemap_face": cubemap_face_key,
            "path": face_paths_by_slot.get(slot, ""),
        })
        cube_faces_meta[slot] = {
            "slot": slot,
            "m_texture": face_paths_by_slot.get(slot, ""),
            "payload_index": face_index,
            "cubemap_face": cubemap_face_key,
        }

    missing_faces = [k for k in _CUBEMAP_FACE_KEYS if k not in face_images]
    if missing_faces:
        raise ValueError(f"Uncooked cubemap face mapping incomplete, missing faces: {missing_faces}")

    meta = {
        "source_path": path,
        "exists": True,
        "ccube_texture": {
            "chunk_type": "CCubeTexture",
            "runtime_fields": {},
        },
        "cube_faces": cube_faces_meta,
        "has_face_metadata": False,
        "is_uncooked_source": True,
        "has_runtime_payload": False,
        "errors": [],
        "uncooked_raw": {
            "reader": "raw_rgba8_full_mips_tail_payload",
            "edge": edge,
            "mipmap_count": mip_count,
            "payload_start": payload_start,
            "payload_bytes": len(payload),
            "face_full_size": face_full_size,
            "top_mip_size": top_size,
            "alpha_channel_index": alpha_index,
            "input_byte_labels": input_labels,
            "slot_order": list(slot_order),
            "ordered_paths": ordered_paths,
            "payload_faces": payload_faces,
        },
    }
    return face_images, meta


def _parse_rgba8_cubemap_dds_top_faces(dds_path: str):
    """Return `(width, height, mask_kind, {face_key: bytes})` for legacy RGBA8 cubemap DDS.

    `mask_kind` is `"bgra"` or `"rgba"` describing how bytes are stored in the DDS.
    Returns `None` if the DDS is not a legacy uncompressed 32-bit cubemap.
    """
    try:
        raw = Path(dds_path).read_bytes()
    except Exception:
        return None
    if len(raw) < 128 or raw[:4] != b"DDS ":
        return None

    try:
        height = struct.unpack_from("<I", raw, 12)[0]
        width = struct.unpack_from("<I", raw, 16)[0]
        mip_count = struct.unpack_from("<I", raw, 28)[0] or 1
        fourcc = struct.unpack_from("<I", raw, 84)[0]
        rgb_bit_count = struct.unpack_from("<I", raw, 88)[0]
        r_mask = struct.unpack_from("<I", raw, 92)[0]
        g_mask = struct.unpack_from("<I", raw, 96)[0]
        b_mask = struct.unpack_from("<I", raw, 100)[0]
        a_mask = struct.unpack_from("<I", raw, 104)[0]
        caps2 = struct.unpack_from("<I", raw, 112)[0]
    except Exception:
        return None

    if fourcc != 0 or rgb_bit_count != 32:
        return None
    if width <= 0 or height <= 0 or width != height:
        return None
    if (caps2 & 0x0000FE00) == 0:  # cubemap face bits missing
        return None

    if (r_mask, g_mask, b_mask, a_mask) == (0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000):
        mask_kind = "bgra"
    elif (r_mask, g_mask, b_mask, a_mask) == (0x000000FF, 0x0000FF00, 0x00FF0000, 0xFF000000):
        mask_kind = "rgba"
    else:
        return None

    sizes = []
    w = width
    h = height
    for _ in range(mip_count):
        sizes.append(max(1, w) * max(1, h) * 4)
        w = max(1, w // 2)
        h = max(1, h // 2)
    face_total = sum(sizes)
    if 128 + (face_total * 6) > len(raw):
        return None

    top_size = sizes[0]
    faces = {}
    data_off = 128
    for face_index, face_key in enumerate(_CUBEMAP_FACE_KEYS):
        face_off = data_off + face_index * face_total
        faces[face_key] = bytes(raw[face_off:face_off + top_size])
        if len(faces[face_key]) != top_size:
            return None

    return width, height, mask_kind, faces


def _parse_block_compressed_cubemap_dds_top_faces(dds_path: str):
    """Return `(width, height, eformat, {face_key: bytes})` for BC cubemap DDS (top mip only)."""
    try:
        raw = Path(dds_path).read_bytes()
    except Exception:
        return None
    if len(raw) < 128 or raw[:4] != b"DDS ":
        return None

    try:
        height = struct.unpack_from("<I", raw, 12)[0]
        width = struct.unpack_from("<I", raw, 16)[0]
        mip_count = struct.unpack_from("<I", raw, 28)[0] or 1
        fourcc = struct.unpack_from("<I", raw, 84)[0]
        caps2 = struct.unpack_from("<I", raw, 112)[0]
    except Exception:
        return None

    if width <= 0 or height <= 0 or width != height:
        return None
    if (caps2 & 0x0000FE00) == 0:
        return None

    # Legacy DXT1/DXT5 DDS or DX10 DDS.
    eformat = None
    block_bytes = None
    data_off = 128
    if fourcc == 0x31545844:  # DXT1
        eformat = EFormat.BC1_UNORM
        block_bytes = 8
    elif fourcc == 0x35545844:  # DXT5
        eformat = EFormat.BC3_UNORM
        block_bytes = 16
    elif fourcc == 0x30315844:  # DX10
        if len(raw) < 148:
            return None
        data_off = 148
        try:
            dxgi_format = struct.unpack_from("<I", raw, 128)[0]
        except Exception:
            return None
        if dxgi_format == 71:
            eformat = EFormat.BC1_UNORM
            block_bytes = 8
        elif dxgi_format == 74:
            eformat = EFormat.BC2_UNORM
            block_bytes = 16
        elif dxgi_format == 77:
            eformat = EFormat.BC3_UNORM
            block_bytes = 16
        elif dxgi_format == 98:
            eformat = EFormat.BC7_UNORM
            block_bytes = 16
        else:
            return None
    else:
        return None

    def mip_size_bytes(w, h):
        bw = max(1, (max(1, w) + 3) // 4)
        bh = max(1, (max(1, h) + 3) // 4)
        return bw * bh * block_bytes

    sizes = []
    w = width
    h = height
    for _ in range(mip_count):
        sizes.append(mip_size_bytes(w, h))
        w = max(1, w // 2)
        h = max(1, h // 2)
    face_total = sum(sizes)
    if data_off + (face_total * 6) > len(raw):
        return None

    top_size = sizes[0]
    faces = {}
    for face_index, face_key in enumerate(_CUBEMAP_FACE_KEYS):
        face_off = data_off + face_index * face_total
        face_bytes = bytes(raw[face_off:face_off + top_size])
        if len(face_bytes) != top_size:
            return None
        faces[face_key] = face_bytes
    return width, height, eformat, faces


def _swizzle_rgba_to_bgra(raw_bytes: bytes) -> bytes:
    if not raw_bytes:
        return raw_bytes
    src = memoryview(raw_bytes)
    out = bytearray(len(raw_bytes))
    for i in range(0, len(raw_bytes), 4):
        out[i + 0] = src[i + 2]
        out[i + 1] = src[i + 1]
        out[i + 2] = src[i + 0]
        out[i + 3] = src[i + 3]
    return bytes(out)


def _write_rgba8_face_dds(face_path: str, width: int, height: int, bgra_bytes: bytes) -> str:
    os.makedirs(os.path.dirname(face_path), exist_ok=True)
    safe_path = win_safe_path(face_path)
    stream = bStream(path=safe_path)
    stream.decoder = 'ISO-8859-1'
    metadata = DDSMetadata(
        width=width,
        height=height,
        mipscount=0,
        format=EFormat.R8G8B8A8_UNORM,
        iscubemap=False,
        slicecount=0,
        normal=False,
    )
    DDSUtils.GenerateAndWriteHeader(stream, metadata)
    stream.write(bgra_bytes)
    stream.close()
    return face_path


def _write_face_dds(face_path: str, width: int, height: int, eformat, face_bytes: bytes) -> str:
    os.makedirs(os.path.dirname(face_path), exist_ok=True)
    safe_path = win_safe_path(face_path)
    stream = bStream(path=safe_path)
    stream.decoder = 'ISO-8859-1'
    metadata = DDSMetadata(
        width=width,
        height=height,
        mipscount=0,
        format=eformat,
        iscubemap=False,
        slicecount=0,
        normal=False,
    )
    DDSUtils.GenerateAndWriteHeader(stream, metadata)
    stream.write(face_bytes)
    stream.close()
    return face_path


def _export_cubemap_face_dds_files(dds_path: str):
    """Export 6 preview DDS files (one per face) from a cubemap DDS.

    Supports legacy uncompressed 32-bit cubemap DDS (RGBA/BGRA masks).
    Returns `{face_key: filepath}` or `None`.
    """
    parsed = _parse_rgba8_cubemap_dds_top_faces(dds_path)
    stem = Path(dds_path).stem
    face_files = {}
    if parsed:
        width, height, mask_kind, faces = parsed
        for face_key in _CUBEMAP_FACE_KEYS:
            face_bytes = faces[face_key]
            # DDSUtils writes A8R8G8B8 masks for R8G8B8A8_UNORM, so write BGRA bytes.
            if mask_kind == "rgba":
                face_bytes = _swizzle_rgba_to_bgra(face_bytes)
            w2_suffix = _CUBEMAP_FACE_W2_SUFFIX.get(face_key, face_key.lower())
            face_path = str(Path(dds_path).with_name(f"{stem}__{w2_suffix}.dds"))
            face_files[face_key] = _write_rgba8_face_dds(face_path, width, height, face_bytes)
        return face_files

    # Support BC-compressed cubemap previews (e.g. uncooked TCM_DXTAlpha -> BC3).
    parsed_bc = _parse_block_compressed_cubemap_dds_top_faces(dds_path)
    if not parsed_bc:
        return None

    width, height, eformat, faces = parsed_bc
    for face_key in _CUBEMAP_FACE_KEYS:
        w2_suffix = _CUBEMAP_FACE_W2_SUFFIX.get(face_key, face_key.lower())
        face_path = str(Path(dds_path).with_name(f"{stem}__{w2_suffix}.dds"))
        face_files[face_key] = _write_face_dds(face_path, width, height, eformat, faces[face_key])

    return face_files


def _build_emission_image_material(mat_name: str, image, label: str = "", mapping_rotation_deg=None):
    mat = bpy.data.materials.new(name=mat_name)
    mat.use_nodes = True
    mat.use_backface_culling = False
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    texcoord = nodes.new(type='ShaderNodeTexCoord')
    texcoord.location = (-760, 0)

    mapping = nodes.new(type='ShaderNodeMapping')
    mapping.location = (-560, 0)
    mapping.label = "Texture Mapping"
    if mapping_rotation_deg is not None:
        try:
            rx, ry, rz = mapping_rotation_deg
            mapping.inputs['Rotation'].default_value = (
                radians(float(rx)),
                radians(float(ry)),
                radians(float(rz)),
            )
        except Exception:
            pass

    tex = nodes.new(type='ShaderNodeTexImage')
    tex.location = (-340, 0)
    tex.image = image
    if label:
        tex.label = label

    emission = nodes.new(type='ShaderNodeEmission')
    emission.location = (-80, 0)
    emission.inputs['Strength'].default_value = 1.0

    out = nodes.new(type='ShaderNodeOutputMaterial')
    out.location = (180, 0)

    links.new(texcoord.outputs['UV'], mapping.inputs['Vector'])
    links.new(mapping.outputs['Vector'], tex.inputs['Vector'])
    links.new(tex.outputs['Color'], emission.inputs['Color'])
    links.new(emission.outputs['Emission'], out.inputs['Surface'])
    return mat


def _cube_face_key_from_normal(normal):
    nx, ny, nz = float(normal.x), float(normal.y), float(normal.z)
    ax, ay, az = abs(nx), abs(ny), abs(nz)
    if ax >= ay and ax >= az:
        return "PX" if nx >= 0.0 else "NX"
    if ay >= ax and ay >= az:
        return "PY" if ny >= 0.0 else "NY"
    return "PZ" if nz >= 0.0 else "NZ"


def _cube_face_uv_from_local(co, face_key: str, half_extent: float):
    half = max(float(half_extent), 1e-8)
    x = float(co.x) / (2.0 * half)
    y = float(co.y) / (2.0 * half)
    z = float(co.z) / (2.0 * half)

    if face_key == "PX":
        u, v = (-y + 0.5), (z + 0.5)
    elif face_key == "NX":
        u, v = (y + 0.5), (z + 0.5)
    elif face_key == "PY":
        u, v = (x + 0.5), (z + 0.5)
    elif face_key == "NY":
        u, v = (-x + 0.5), (z + 0.5)
    elif face_key == "PZ":
        u, v = (x + 0.5), (-y + 0.5)
    else:  # NZ
        u, v = (x + 0.5), (y + 0.5)

    # Clamp tiny floating point drift around [0,1].
    u = 0.0 if u < 0.0 and u > -1e-6 else (1.0 if u > 1.0 and u < 1.0 + 1e-6 else u)
    v = 0.0 if v < 0.0 and v > -1e-6 else (1.0 if v > 1.0 and v < 1.0 + 1e-6 else v)
    return u, v


def _apply_w2cube_face_preview_materials_from_images(obj, stem: str, face_images: dict, face_labels: dict | None = None) -> bool:
    if not face_images:
        return False
    face_labels = face_labels or {}
    mesh = obj.data
    if mesh.uv_layers:
        uv_layer = mesh.uv_layers.active
    else:
        uv_layer = mesh.uv_layers.new(name="UVMap")

    mesh.materials.clear()
    material_index_by_face = {}
    for face_key in _CUBEMAP_FACE_KEYS:
        img = face_images.get(face_key)
        if not img:
            return False
        if img:
            try:
                # File-backed images support reload; generated images do not.
                if getattr(img, "filepath", ""):
                    img.reload()
            except Exception:
                pass
            try:
                img.colorspace_settings.name = 'sRGB'
            except Exception:
                pass
        mat = _build_emission_image_material(
            f"{stem}__{_CUBEMAP_FACE_W2_SUFFIX.get(face_key, face_key.lower())}",
            img,
            label=face_labels.get(face_key, _CUBEMAP_FACE_LABELS.get(face_key, face_key)),
            mapping_rotation_deg=_CUBEMAP_FACE_MAPPING_ROT_DEG.get(face_key),
        )
        mesh.materials.append(mat)
        material_index_by_face[face_key] = len(mesh.materials) - 1

    half_extent = 0.0
    for v in mesh.vertices:
        half_extent = max(half_extent, abs(v.co.x), abs(v.co.y), abs(v.co.z))
    half_extent = max(half_extent, 1e-6)

    uv_data = uv_layer.data
    loops = mesh.loops
    verts = mesh.vertices
    for poly in mesh.polygons:
        face_key = _cube_face_key_from_normal(poly.normal)
        poly.material_index = material_index_by_face.get(face_key, 0)
        for li in poly.loop_indices:
            vert = verts[loops[li].vertex_index]
            uv_data[li].uv = _cube_face_uv_from_local(vert.co, face_key, half_extent)

    return True


def _apply_w2cube_face_preview_materials(obj, stem: str, dds_path: str) -> bool:
    face_files = _export_cubemap_face_dds_files(dds_path) if dds_path else None
    if not face_files:
        return False

    face_images = {}
    face_labels = {}
    for face_key in _CUBEMAP_FACE_KEYS:
        face_path = face_files.get(face_key)
        if not face_path:
            return False
        img = bpy_image_load_safe(face_path, check_existing=True)
        if not img:
            return False
        face_images[face_key] = img
        face_labels[face_key] = f"{Path(face_path).name} ({_CUBEMAP_FACE_LABELS.get(face_key, face_key)})"

    return _apply_w2cube_face_preview_materials_from_images(obj, stem, face_images, face_labels=face_labels)


def _apply_w2cube_env_preview_material(obj, source_path: str, image):
    stem = Path(source_path).stem or "cubemap"
    mat = bpy.data.materials.new(name=f"{stem}_w2cube")
    mat.use_nodes = True
    mat.use_backface_culling = False
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    texcoord = nodes.new(type='ShaderNodeTexCoord')
    texcoord.location = (-900, 0)

    normalize = nodes.new(type='ShaderNodeVectorMath')
    normalize.operation = 'NORMALIZE'
    normalize.location = (-700, 0)

    env = nodes.new(type='ShaderNodeTexEnvironment')
    env.location = (-480, 0)
    env.width = 260
    env.image = image
    env.label = os.path.basename(source_path)

    emission = nodes.new(type='ShaderNodeEmission')
    emission.location = (-220, 0)
    emission.inputs['Strength'].default_value = 1.0

    output = nodes.new(type='ShaderNodeOutputMaterial')
    output.location = (20, 0)

    links.new(texcoord.outputs['Object'], normalize.inputs[0])
    links.new(normalize.outputs[0], env.inputs['Vector'])
    links.new(env.outputs[0], emission.inputs[0])
    links.new(emission.outputs[0], output.inputs['Surface'])

    obj.data.materials.append(mat)
    return mat


def _create_w2cube_preview_object(context, source_path: str, image, dds_path: str, cube_size: float = 2.0, face_images: dict | None = None):
    stem = Path(source_path).stem or "cubemap"
    obj_name = f"w2cube_{stem}"
    target_collection = _dev_get_link_collection(context)
    if target_collection is None:
        raise RuntimeError("No target collection available in current context")

    mesh = bpy.data.meshes.new(f"{obj_name}_mesh")

    bm = bmesh.new()
    try:
        bmesh.ops.create_cube(bm, size=float(cube_size))
        bm.to_mesh(mesh)
    finally:
        bm.free()

    obj = bpy.data.objects.new(obj_name, mesh)
    target_collection.objects.link(obj)

    # Place at the 3D cursor if available.
    cursor = getattr(getattr(context, "scene", None), "cursor", None)
    if cursor is not None:
        try:
            obj.location = cursor.location.copy()
        except Exception:
            pass

    mat = None
    used_face_preview = False
    if face_images:
        try:
            used_face_preview = _apply_w2cube_face_preview_materials_from_images(obj, stem, face_images)
        except Exception:
            log.exception("Failed to build square-face cubemap preview from uncooked face images for %s", source_path)
    try:
        if not used_face_preview:
            used_face_preview = _apply_w2cube_face_preview_materials(obj, stem, dds_path)
    except Exception:
        log.exception("Failed to build square-face cubemap preview from %s", dds_path)

    if used_face_preview:
        mat = obj.data.materials[0] if obj.data.materials else None
    else:
        if image is None:
            raise RuntimeError("No cubemap face preview or environment image available")
        mat = _apply_w2cube_env_preview_material(obj, source_path, image)
    obj["witcher_w2cube_path"] = source_path
    if dds_path:
        obj["witcher_w2cube_dds"] = dds_path

    # Best-effort selection/activation without relying on 3D view operators.
    try:
        for selected in getattr(context, "selected_objects", []) or []:
            selected.select_set(False)
        obj.select_set(True)
    except Exception:
        pass
    try:
        if getattr(context, "view_layer", None):
            context.view_layer.objects.active = obj
    except Exception:
        pass

    return obj, mat

class WITCH_OT_w2mg(bpy.types.Operator, ImportHelper):
    """Load Witcher 3 Material Shader"""
    bl_idname = "witcher.import_w2mg"
    bl_label = "Import .w2mg"
    filename_ext = ".w2mg"
    filter_glob: StringProperty(default='*.w2mg', options={'HIDDEN'})
    do_update_mats: BoolProperty(
        name="Material Update",
        default=True,
        description="If enabled, it will replace the material with same name instead of creating a new one"
    )
    def execute(self, context):
        log.debug("importing material")
        fdir = self.filepath
        if os.path.isdir(fdir):
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}
        ext = file_helpers.getFilenameType(fdir)
        if ext == ".w2mg":
            w3_material_blender.import_w2mg(fdir, self)
        else:
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}
        return {'FINISHED'}
    
    def invoke(self, context, event):
        UNCOOK_PATH = get_uncook_path(context) + '\\'
        if os.path.exists(UNCOOK_PATH):
            self.filepath = UNCOOK_PATH if self.filepath == '' else self.filepath
        return ImportHelper.invoke(self, context, event)

class WITCH_OT_w2mi(bpy.types.Operator, ImportHelper):
    """Load Witcher 3 Material Instance"""
    bl_idname = "witcher.import_w2mi"
    bl_label = "Import .w2mi"
    filename_ext = ".w2mi"
    filter_glob: StringProperty(default='*.w2mi', options={'HIDDEN'})
    do_update_mats: BoolProperty(
        name="Material Update",
        default=True,
        description="If enabled, it will replace the material with same name instead of creating a new one"
    )
    def execute(self, context):
        log.debug("importing material instance")
        fdir = self.filepath
        if os.path.isdir(fdir):
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}
        ext = file_helpers.getFilenameType(fdir)
        if ext == ".w2mi":
            bpy.ops.mesh.primitive_plane_add()
            obj = bpy.context.selected_objects[:][0]
            instance_filename = Path(fdir).stem
            materials = []
            material_file_chunks = CR2W.CR2W_reader.load_material(fdir)
            for idx, mat in enumerate(material_file_chunks):
                # if idx > 0:
                #     raise Exception('wut')
                target_mat = False
                if self.do_update_mats:
                    if instance_filename in obj.data.materials:
                        target_mat = obj.data.materials[instance_filename] #None
                    if instance_filename in bpy.data.materials:
                        target_mat = bpy.data.materials[instance_filename] #None
                if not target_mat:
                    target_mat = bpy.data.materials.new(name=instance_filename)

                finished_mat = setup_w3_material_CR2W(get_texture_path(context), target_mat, mat, force_update=True, mat_filename=instance_filename, is_instance_file = True)

                if instance_filename in obj.data.materials and not self.do_update_mats:
                    obj.material_slots[target_mat.name].material = finished_mat
                else:
                    obj.data.materials.append(finished_mat)
        else:
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}
        return {'FINISHED'}
    
    def invoke(self, context, event):
        UNCOOK_PATH = get_uncook_path(context) + '\\'
        if os.path.exists(UNCOOK_PATH):
            self.filepath = UNCOOK_PATH if self.filepath == '' else self.filepath
        return ImportHelper.invoke(self, context, event)

class WITCH_OT_xbm(bpy.types.Operator, ImportHelper):
    """Load Witcher 2 Texture"""
    bl_idname = "witcher.import_xbm"
    bl_label = "Import .xbm"
    filename_ext = ".xbm"
    filter_glob: StringProperty(default='*.xbm', options={'HIDDEN'})
    def execute(self, context):
        log.debug("importing xbm")
        fdir = self.filepath
        if os.path.isdir(fdir):
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}
        ext = file_helpers.getFilenameType(fdir)
        if ext == ".xbm":
            dds_path = convert_xbm_to_dds(fdir)
            image = bpy_image_load_safe(dds_path, check_existing=True)
            try:
                from ..ui.ui_texture_export import apply_texture_image_metadata

                apply_texture_image_metadata(context, image, fdir)
            except Exception:
                log.exception("Failed to seed image metadata from imported XBM: %s", fdir)
                    
        else:
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}
        return {'FINISHED'}
    
    def invoke(self, context, event):
        UNCOOK_PATH = get_uncook_path(context) + '\\'
        if os.path.exists(UNCOOK_PATH):
            self.filepath = UNCOOK_PATH if self.filepath == '' else self.filepath
        return ImportHelper.invoke(self, context, event)


class WITCH_OT_w2cube(bpy.types.Operator, ImportHelper):
    """Load Witcher 3 Cubemap as a preview cube"""
    bl_idname = "witcher.import_w2cube"
    bl_label = "Import .w2cube"
    filename_ext = ".w2cube"
    filter_glob: StringProperty(default='*.w2cube', options={'HIDDEN'})
    cube_size: FloatProperty(
        name="Cube Size",
        description="Size of the generated preview cube",
        default=2.0,
        min=0.001,
        soft_min=0.1,
        soft_max=100.0,
    )

    def execute(self, context):
        log.debug("importing w2cube")
        fdir = self.filepath
        if os.path.isdir(fdir):
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}

        ext = file_helpers.getFilenameType(fdir)
        if ext != ".w2cube":
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}

        meta_sources = []
        preview_path = fdir
        selected_is_uncooked_by_name = str(fdir).lower().endswith("_uncooked.w2cube")
        selected_is_uncooked_raw_tail = False
        uncooked_face_images = None
        uncooked_raw_meta = None

        if not selected_is_uncooked_by_name:
            try:
                selected_is_uncooked_raw_tail = _is_probable_uncooked_w2cube_raw_tail_file(fdir)
            except Exception:
                selected_is_uncooked_raw_tail = False
            if selected_is_uncooked_raw_tail:
                log.info("w2cube content probe detected uncooked raw-tail cubemap: %s", fdir)

        selected_meta = None
        # Uncooked `.w2cube` parsing can be substantially heavier (and has been
        # observed to hang on some files). Prefer getting a preview first.
        if not (selected_is_uncooked_by_name or selected_is_uncooked_raw_tail):
            # Always try to read metadata from the selected file first. For
            # cooked files this captures runtime payload fields.
            log.debug("w2cube reading metadata: %s", fdir)
            selected_meta = _read_w2cube_chunk_metadata(fdir)
            if selected_meta and not selected_meta.get("errors"):
                meta_sources.append(selected_meta)
            elif selected_meta and selected_meta.get("errors"):
                log.info("w2cube metadata parse notes for %s: %s", fdir, selected_meta.get("errors"))
        else:
            log.info("w2cube skipping eager metadata parse for uncooked source: %s", fdir)

        # Prefer a direct uncooked import path for `_uncooked.w2cube` files.
        # Pairing is still useful when importing cooked files (to fetch uncooked
        # face metadata), but uncooked selection should not silently switch to a
        # different file for preview generation.
        if selected_is_uncooked_by_name or selected_is_uncooked_raw_tail or (selected_meta and selected_meta.get("is_uncooked_source")):
            log.info("w2cube uncooked source selected; using shared DDS export workflow (no auto-pair)")
        else:
            uncooked_pair = _pair_uncooked_from_cooked_w2cube(fdir)
            if uncooked_pair:
                uncooked_meta = _read_w2cube_chunk_metadata(uncooked_pair)
                if uncooked_meta and not uncooked_meta.get("errors"):
                    meta_sources.append(uncooked_meta)

        # Persist metadata immediately so the user can inspect it even if preview
        # conversion/import fails later in this operator.
        meta_text_by_source = {}
        for meta in meta_sources:
            stem = Path(meta.get("source_path") or fdir).stem
            text_name = _write_w2cube_metadata_text(meta, name_hint=f"w2cube_meta_{stem}")
            if text_name:
                meta_text_by_source[str(meta.get("source_path") or "")] = text_name

        img = None
        dds_path = ""
        if uncooked_face_images:
            try:
                obj, _mat = _create_w2cube_preview_object(
                    context,
                    source_path=preview_path,
                    image=None,
                    dds_path="",
                    cube_size=self.cube_size,
                    face_images=uncooked_face_images,
                )
            except Exception as e:
                log.exception("Failed to build uncooked w2cube preview object: %s", preview_path)
                self.report({'ERROR'}, f"Failed to create uncooked preview cube: {e}")
                return {'CANCELLED'}
        else:
            try:
                log.debug("w2cube loading preview image from: %s", preview_path)
                img, dds_path = load_w2cube_image(preview_path, colorspace='sRGB')
            except Exception as e:
                log.exception("Failed to convert/load w2cube: %s", preview_path)
                self.report({'ERROR'}, f"Failed to load cubemap: {e}")
                return {'CANCELLED'}

            if not img:
                self.report({'ERROR'}, "Failed to load cubemap image from .w2cube")
                return {'CANCELLED'}

            try:
                obj, _mat = _create_w2cube_preview_object(
                    context,
                    source_path=preview_path,
                    image=img,
                    dds_path=dds_path or "",
                    cube_size=self.cube_size,
                )
            except Exception as e:
                log.exception("Failed to build w2cube preview object: %s", preview_path)
                self.report({'ERROR'}, f"Failed to create preview cube: {e}")
                return {'CANCELLED'}

        # Write metadata to Blender Text datablocks and attach the most useful
        # (prefer uncooked CubeFace metadata) to the imported object.
        preferred_meta = None
        for meta in meta_sources:
            if preferred_meta is None or meta.get("is_uncooked_source"):
                preferred_meta = meta
        if preferred_meta:
            preferred_text = meta_text_by_source.get(str(preferred_meta.get("source_path") or ""), "")
            _attach_w2cube_metadata_to_object(
                obj,
                preferred_meta,
                text_name=preferred_text,
            )
            if preferred_meta.get("is_uncooked_source"):
                face_flags = {}
                for slot, face in (preferred_meta.get("cube_faces") or {}).items():
                    face_flags[slot] = {
                        "rotate": face.get("m_rotate"),
                        "flipX": face.get("m_flipX"),
                        "flipY": face.get("m_flipY"),
                    }
                log.info("w2cube uncooked face flags for %s: %s", preferred_meta.get("source_path"), face_flags)
            if selected_meta and selected_meta.get("is_uncooked_source"):
                obj["witcher_w2cube_uncooked_source"] = fdir
                obj["witcher_w2cube_preview_source"] = preview_path
            if uncooked_raw_meta and preferred_meta is uncooked_raw_meta:
                raw_info = uncooked_raw_meta.get("uncooked_raw") or {}
                try:
                    obj["witcher_w2cube_uncooked_raw"] = True
                    obj["witcher_w2cube_uncooked_edge"] = int(raw_info.get("edge") or 0)
                    obj["witcher_w2cube_uncooked_mips"] = int(raw_info.get("mipmap_count") or 0)
                    obj["witcher_w2cube_uncooked_payload_start"] = int(raw_info.get("payload_start") or 0)
                    obj["witcher_w2cube_uncooked_byte_labels"] = str(raw_info.get("input_byte_labels") or "")
                except Exception:
                    pass

        if preview_path != fdir:
            self.report({'INFO'}, f"Imported cubemap preview: {obj.name} (cooked pair + uncooked metadata)")
        elif uncooked_face_images:
            self.report({'INFO'}, f"Imported uncooked cubemap preview: {obj.name}")
        else:
            self.report({'INFO'}, f"Imported cubemap preview: {obj.name}")
        return {'FINISHED'}

    def invoke(self, context, event):
        UNCOOK_PATH = get_uncook_path(context) + '\\'
        if os.path.exists(UNCOOK_PATH):
            self.filepath = UNCOOK_PATH if self.filepath == '' else self.filepath
        return ImportHelper.invoke(self, context, event)


from bpy.utils import (register_class, unregister_class)

_classes= [
    WITCH_OT_xbm,
    WITCH_OT_w2cube,
    WITCH_OT_w2mi,
    WITCH_OT_w2mg,
]

def register():
    for cls in _classes:
        register_class(cls)

def unregister():
    for cls in _classes:
        unregister_class(cls)
