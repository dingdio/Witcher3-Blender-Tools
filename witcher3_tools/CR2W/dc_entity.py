import copy
import logging
import re
from typing import List
log = logging.getLogger(__name__)

import os
from pathlib import Path

from .common_blender import repo_file
from .CR2W_file import create_level, read_CR2W
from .CR2W_types import Entity_Type_List, getCR2W
from .bStream import bStream
from .read_json_w3 import readCSkeletonData
from . import w3_types

# Session-scoped cache for LoadCEntityTemplateFile results.
# Cleared between imports via clear_template_cache().
_template_file_cache = {}

_DEPOT_PATH_ROOTS = (
    "templates",
    "game",
    "characters",
    "items",
    "dlc",
    "environment",
    "quests",
    "levels",
    "living_world",
    "gameplay",
    "animations",
    "fx",
    "engine",
    "globals",
    "gui",
    "ui",
)
_KNOWN_REPO_EXTS = (
    ".w2mesh",
    ".w2rig",
    ".w2anims",
    ".w2beh",
    ".w2steer",
    ".w2ent",
    ".redcloth",
    ".w3fac",
    ".w2fac",
    ".w3dyng",
    ".dyng",
)

def clear_template_cache():
    """Clear the template file parse cache. Call at the start of each import."""
    _template_file_cache.clear()


def _repo_path_key(path: str) -> str:
    return str(path or "").replace("/", "\\").strip().lower()


def _extract_depot_subpath(path_value):
    if not isinstance(path_value, str):
        return None
    normalized = path_value.strip().replace("/", "\\")
    if not normalized:
        return None
    lowered = normalized.lower()
    best_idx = None
    for root in _DEPOT_PATH_ROOTS:
        marker = f"{root}\\"
        idx = lowered.find(marker)
        if idx < 0:
            continue
        if best_idx is None or idx < best_idx:
            best_idx = idx
    if best_idx is not None:
        return normalized[best_idx:]
    return None


def _is_valid_repo_path(path_value, expected_ext: str | None = None) -> bool:
    if not isinstance(path_value, str):
        return False
    candidate = _extract_depot_subpath(path_value)
    if not candidate:
        return False
    if any(ord(ch) < 32 for ch in candidate):
        return False
    lowered = candidate.lower()
    if lowered.startswith(("array:", "handle:", "ptr:")):
        return False
    if expected_ext:
        return lowered.endswith(expected_ext.lower())
    return any(lowered.endswith(ext) for ext in _KNOWN_REPO_EXTS)


def _path_candidate_exts(expected_ext: str):
    ext = str(expected_ext or "").lower()
    candidate_map = {
        ".w2mesh": (".w2mesh", ".lmf", ".mmm"),
        ".w2rig": (".w2rig", ".hkx"),
        ".w3dyng": (".w3dyng", ".dyng"),
        ".dyng": (".dyng", ".w3dyng"),
    }
    return candidate_map.get(ext, (ext,))


def _has_candidate_ext(path_value, expected_ext: str) -> bool:
    if not isinstance(path_value, str):
        return False
    lowered = path_value.lower()
    return any(lowered.endswith(ext) for ext in _path_candidate_exts(expected_ext))


def _candidate_import_indices(import_index):
    try:
        idx = int(import_index)
    except Exception:
        return []
    if idx >= 0x80000000:
        idx -= 0x100000000
    if idx < 0:
        idx = -idx - 1
    if idx < 0:
        return []
    out = [idx]
    if idx > 0:
        out.append(idx - 1)
    return out


def _template_cache_key(template_filename: str) -> str:
    return str(template_filename or "").lower().replace("/", "\\")


def _normalize_repo_subpath(depot_subpath: str, expected_ext: str):
    normalized = depot_subpath.replace("/", "\\").lstrip("\\")
    if expected_ext.lower() == ".w2mesh":
        normalized = re.sub(r"(?i)\\export\\", r"\\model\\", normalized, count=1)
    root, _ = os.path.splitext(normalized)
    normalized = root + expected_ext
    return normalized if _is_valid_repo_path(normalized, expected_ext) else None


def _repo_path_candidates(path_value, expected_ext: str):
    if not isinstance(path_value, str):
        return []
    normalized = path_value.replace("/", "\\")
    out = []
    seen = set()

    direct = _extract_depot_subpath(normalized)
    if direct and _has_candidate_ext(direct, expected_ext):
        key = _repo_path_key(direct)
        seen.add(key)
        out.append(direct)

    pattern = re.compile(
        r"(?i)(?:"
        + "|".join(re.escape(root) for root in _DEPOT_PATH_ROOTS)
        + r")[\\][A-Za-z0-9_./\\-]{0,260}?(?:"
        + "|".join(re.escape(ext) for ext in _path_candidate_exts(expected_ext))
        + r")"
    )
    for match in pattern.finditer(normalized):
        candidate = match.group(0).replace("/", "\\")
        key = _repo_path_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out


def _normalize_repo_path_value(path_value, expected_ext: str):
    for candidate in _repo_path_candidates(path_value, expected_ext):
        normalized = _normalize_repo_subpath(candidate, expected_ext)
        if normalized:
            return normalized
    return None


def _source_repo_roots_for_chunk(chunk):
    cr2w_file = getattr(chunk, "_W_CLASS__CR2WFILE", None) if chunk else None
    file_name = getattr(cr2w_file, "fileName", None)
    if not file_name or not os.path.isabs(file_name):
        return []
    norm_path = os.path.normpath(file_name)
    lower_path = norm_path.lower()
    markers = ("\\game\\", "\\templates\\") + tuple(f"\\{root}\\" for root in _DEPOT_PATH_ROOTS)
    out = []
    seen = set()
    for marker in markers:
        idx = lower_path.find(marker)
        if idx <= 2:
            continue
        root = norm_path[:idx]
        norm_root = os.path.normcase(os.path.normpath(root))
        if norm_root in seen:
            continue
        seen.add(norm_root)
        out.append(root)
    parent_dir = os.path.dirname(norm_path)
    if parent_dir:
        norm_parent = os.path.normcase(os.path.normpath(parent_dir))
        if norm_parent not in seen:
            seen.add(norm_parent)
            out.append(parent_dir)
    return out


def _repo_path_exists(chunk, repo_path: str) -> bool:
    if not chunk or not repo_path:
        return False
    cr2w_file = getattr(chunk, "_W_CLASS__CR2WFILE", None)
    version = getattr(getattr(cr2w_file, "HEADER", None), "version", 999)
    try:
        resolved = repo_file(repo_path, version)
    except Exception:
        resolved = ""
    if resolved and os.path.exists(resolved):
        return True
    rel_path = repo_path.replace("/", "\\").lstrip("\\")
    for root in _source_repo_roots_for_chunk(chunk):
        if os.path.exists(os.path.join(root, rel_path)):
            return True
    return False


def _canonical_component_name(chunk_name: str) -> str:
    name = str(chunk_name or "").strip().lower()
    if not name:
        return ""
    if name.startswith("mesh_"):
        name = name[5:]
    name = re.sub(r"[^a-z0-9_]+", "_", name).strip("_")
    return name


def _repair_w2_component_mesh_path(chunk, repo_path: str):
    if not chunk or not repo_path:
        return repo_path
    cr2w_file = getattr(chunk, "_W_CLASS__CR2WFILE", None)
    version = getattr(getattr(cr2w_file, "HEADER", None), "version", 999)
    if version > 115 or _repo_path_exists(chunk, repo_path):
        return repo_path

    component_name = _canonical_component_name(_prop_to_string(_find_prop_by_name(chunk, "name")))
    if not component_name:
        return repo_path

    directory, filename = os.path.split(repo_path.replace("/", "\\"))
    stem, ext = os.path.splitext(filename)
    prefix, sep, suffix = stem.rpartition("_")
    if not sep or not prefix or not suffix:
        return repo_path
    if f"__{component_name}_" in stem.lower():
        return repo_path

    candidate = os.path.join(directory, f"{prefix}__{component_name}_{suffix}{ext}").replace("/", "\\")
    if _repo_path_exists(chunk, candidate):
        return candidate
    return repo_path


def _prop_to_string(prop):
    if not prop:
        return None
    try:
        value = prop.ToString()
    except Exception:
        value = None
    if hasattr(value, "value"):
        value = value.value
    if isinstance(value, str):
        value = value.strip()
        return value or None

    if hasattr(prop, "String"):
        value = getattr(prop, "String", None)
        if isinstance(value, str):
            value = value.strip()
            return value or None

    index = getattr(prop, "Index", None)
    if index is not None and not isinstance(index, list):
        value = getattr(index, "String", None) or getattr(index, "value", None)
        if isinstance(value, str):
            value = value.strip()
            return value or None
    return None


def _resolve_repo_path_from_import_index(cr2w_file, import_index, expected_ext: str):
    if not cr2w_file:
        return None
    imports = getattr(cr2w_file, "CR2WImport", None) or []
    for candidate_idx in _candidate_import_indices(import_index):
        if 0 <= candidate_idx < len(imports):
            imp = imports[candidate_idx]
            raw_path = getattr(imp, "path", None) or getattr(imp, "DepotPath", None)
            candidate = _normalize_repo_path_value(raw_path, expected_ext)
            if candidate:
                return candidate
    return None


def _resolve_handle_repo_path(chunk, handle, expected_ext: str):
    if not handle:
        return None
    cr2w_file = getattr(chunk, "_W_CLASS__CR2WFILE", None) if chunk else None
    direct = _normalize_repo_path_value(getattr(handle, "DepotPath", None), expected_ext)
    if direct:
        return direct

    if getattr(handle, "ChunkHandle", False) and cr2w_file:
        ref_idx = getattr(handle, "Reference", None)
        if isinstance(ref_idx, int) and 0 <= ref_idx < len(cr2w_file.CHUNKS.CHUNKS):
            ref_chunk = cr2w_file.CHUNKS.CHUNKS[ref_idx]
            for prop_name in ("importFile", "resource", "mesh", "skeleton", "mimicFace", "dyng"):
                candidate = _resolve_repo_path(ref_chunk, prop_name, expected_ext)
                if candidate:
                    return candidate

    raw_val = getattr(handle, "val", None)
    if isinstance(raw_val, int) and raw_val < 0:
        candidate = _resolve_repo_path_from_import_index(cr2w_file, -raw_val - 1, expected_ext)
        if candidate:
            return candidate

    idx = getattr(handle, "Index", None)
    if idx is not None:
        candidate = _resolve_repo_path_from_import_index(cr2w_file, idx, expected_ext)
        if candidate:
            return candidate
    return None


def _resolve_repo_path(chunk, prop_name: str, expected_ext: str):
    if not chunk:
        return None
    try:
        prop = chunk.GetVariableByName(prop_name)
    except Exception:
        prop = None
    if not prop:
        return None

    candidate = _normalize_repo_path_value(_prop_to_string(prop), expected_ext)
    if candidate:
        return candidate

    for handle in getattr(prop, "Handles", None) or []:
        candidate = _resolve_handle_repo_path(chunk, handle, expected_ext)
        if candidate:
            return candidate

    idx = getattr(prop, "Index", None)
    if idx is not None:
        candidate = _resolve_repo_path_from_import_index(
            getattr(chunk, "_W_CLASS__CR2WFILE", None),
            idx,
            expected_ext,
        )
        if candidate:
            return candidate
    return None


def _resolve_repo_paths_from_array(chunk, prop_name: str, expected_ext: str):
    if not chunk:
        return []
    try:
        prop = chunk.GetVariableByName(prop_name)
    except Exception:
        prop = None
    if not prop:
        return []

    out = []
    seen = set()
    handles = getattr(prop, "Handles", None) or []
    for handle in handles:
        candidate = _resolve_handle_repo_path(chunk, handle, expected_ext)
        if not candidate:
            continue
        key = _repo_path_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)

    if out:
        return out

    candidate = _normalize_repo_path_value(_prop_to_string(prop), expected_ext)
    if candidate:
        key = _repo_path_key(candidate)
        if key not in seen:
            seen.add(key)
            out.append(candidate)
    return out


def _collect_w2_related_entity_paths(cr2w_file):
    out = []
    seen = set()
    if not cr2w_file:
        return out

    def _add(path_value):
        candidate = _normalize_repo_path_value(path_value, ".w2ent")
        if not candidate:
            return
        key = _repo_path_key(candidate)
        if key in seen:
            return
        seen.add(key)
        out.append(candidate)

    for chunk in getattr(getattr(cr2w_file, "CHUNKS", None), "CHUNKS", None) or []:
        if chunk.Type != "CEntityTemplate":
            continue
        try:
            includes = chunk.GetVariableByName("includes")
        except Exception:
            includes = None
        for handle in getattr(includes, "Handles", None) or []:
            _add(_resolve_handle_repo_path(chunk, handle, ".w2ent"))

    for imp in getattr(cr2w_file, "CR2WImport", None) or []:
        raw_path = getattr(imp, "path", None) or getattr(imp, "DepotPath", None)
        _add(raw_path)
    return out


def _load_w2_related_files_recursive(cr2w_file, inherit_visited):
    out = []
    seen_paths = set(inherit_visited or set())
    queue = [cr2w_file]
    while queue:
        source_file = queue.pop(0)
        for depot_path in _collect_w2_related_entity_paths(source_file):
            try:
                full_path = _resolve_w2_related_full_path(source_file, depot_path)
                norm_full_path = os.path.normcase(os.path.normpath(full_path))
            except Exception:
                continue
            if norm_full_path in seen_paths:
                continue
            seen_paths.add(norm_full_path)
            try:
                related_file = read_CR2W(full_path)
            except Exception:
                continue
            out.append((depot_path, full_path, related_file))
            queue.append(related_file)
    return out


def _w2_repo_roots_from_file_path(file_name: str):
    if not file_name or not os.path.isabs(file_name):
        return []
    norm_path = os.path.normpath(file_name)
    lower_path = norm_path.lower()
    markers = ("\\data\\", "\\game\\", "\\templates\\") + tuple(f"\\{root}\\" for root in _DEPOT_PATH_ROOTS)
    out = []
    seen = set()
    for marker in markers:
        idx = lower_path.find(marker)
        if idx <= 2:
            continue
        if marker == "\\data\\":
            root = norm_path[:idx + len("\\data")]
        else:
            root = norm_path[:idx]
        norm_root = os.path.normcase(os.path.normpath(root))
        if norm_root in seen:
            continue
        seen.add(norm_root)
        out.append(root)
    return out


def _resolve_w2_related_full_path(cr2w_file, repo_path: str):
    if not repo_path:
        return ""
    if os.path.isabs(repo_path):
        return repo_path
    version = getattr(getattr(cr2w_file, "HEADER", None), "version", 999)
    try:
        candidate = repo_file(repo_path, version)
    except Exception:
        candidate = ""
    if candidate and os.path.exists(candidate):
        return candidate
    rel_path = str(repo_path).replace("/", "\\").lstrip("\\")
    fallback = ""
    for root in _w2_repo_roots_from_file_path(getattr(cr2w_file, "fileName", None)):
        candidate = os.path.join(root, rel_path)
        if not fallback:
            fallback = candidate
        if os.path.exists(candidate):
            return candidate
    return candidate or fallback or str(repo_path)

def is_valid_mesh_path(mesh_value) -> bool:
    """Return True when value looks like a real depot mesh path."""
    return _is_valid_repo_path(mesh_value, ".w2mesh")

def _convert_mesh_value(mesh_prop):
    if not mesh_prop:
        return None
    try:
        mesh_value = mesh_prop.ToString() if hasattr(mesh_prop, "ToString") else str(mesh_prop)
    except Exception:
        return None
    return _normalize_repo_path_value(mesh_value, ".w2mesh")

def _convert_color_value(color_prop):
    if not color_prop:
        return None
    if isinstance(color_prop, dict):
        return {
            key: color_prop.get(key)
            for key in ("Red", "Green", "Blue", "Alpha")
            if key in color_prop
        }

    prop_items = getattr(color_prop, "MoreProps", None) or getattr(color_prop, "More", None) or []
    color = {}
    for item in prop_items:
        key = getattr(item, "theName", None)
        value = getattr(item, "Value", None)
        if key in ("Red", "Green", "Blue", "Alpha"):
            color[key] = value

    if color:
        return color

    for key in ("Red", "Green", "Blue", "Alpha"):
        value = getattr(color_prop, key, None)
        if value is not None:
            color[key] = value
    return color or color_prop

def _class_name_from_import(cr2w_file, imp):
    class_name = getattr(imp, "className", None)
    if isinstance(class_name, int):
        try:
            return cr2w_file.CNAMES[class_name].name.value
        except Exception:
            return None
    if hasattr(class_name, "value"):
        try:
            return class_name.value
        except Exception:
            return None
    return class_name

def _collect_mesh_import_paths(cr2w_file):
    """Collect CMesh import depot paths from a CR2W file in import-table order."""
    out = []
    if not cr2w_file:
        return out
    imports = getattr(cr2w_file, "CR2WImport", None) or []
    for imp in imports:
        class_name = _class_name_from_import(cr2w_file, imp)
        if class_name not in (None, "CMesh"):
            continue
        raw_path = getattr(imp, "path", None) or getattr(imp, "DepotPath", None)
        candidate = _normalize_repo_path_value(raw_path, ".w2mesh")
        if candidate:
            out.append(candidate)
    return out


def _collect_rig_import_paths(cr2w_file):
    """Collect CSkeleton import depot paths from a CR2W file in import-table order.
    Used as fallback when a CAnimatedComponent override chunk omits the skeleton property."""
    out = []
    if not cr2w_file:
        return out
    imports = getattr(cr2w_file, "CR2WImport", None) or []
    for imp in imports:
        class_name = _class_name_from_import(cr2w_file, imp)
        if class_name not in (None, "CSkeleton"):
            continue
        raw_path = getattr(imp, "path", None) or getattr(imp, "DepotPath", None)
        candidate = _normalize_repo_path_value(raw_path, ".w2rig")
        if candidate:
            out.append(candidate)
    return out

def _mesh_path_from_import_index(chunk, import_index):
    cr2w_file = getattr(chunk, "_W_CLASS__CR2WFILE", None) if chunk else None
    return _resolve_repo_path_from_import_index(cr2w_file, import_index, ".w2mesh")

def _mesh_path_from_handle(chunk, handle):
    return _resolve_handle_repo_path(chunk, handle, ".w2mesh")

def _resolve_mesh_path(chunk, mesh_value):
    """Resolve a mesh path from parsed chunk data."""
    if is_valid_mesh_path(mesh_value):
        return _repair_w2_component_mesh_path(chunk, mesh_value)
    candidate = _resolve_repo_path(chunk, "mesh", ".w2mesh")
    if candidate:
        return _repair_w2_component_mesh_path(chunk, candidate)
    try:
        mesh_var = chunk.GetVariableByName("mesh") if chunk else None
    except Exception:
        mesh_var = None
    if mesh_var:
        try:
            direct_mesh = mesh_var.ToString()
        except Exception:
            direct_mesh = None
        if is_valid_mesh_path(direct_mesh):
            return _repair_w2_component_mesh_path(chunk, direct_mesh)
        handles = getattr(mesh_var, "Handles", None) or []
        for handle in handles:
            candidate = _mesh_path_from_handle(chunk, handle)
            if is_valid_mesh_path(candidate):
                return _repair_w2_component_mesh_path(chunk, candidate)
        candidate = _mesh_path_from_import_index(chunk, getattr(mesh_var, "Index", None))
        if is_valid_mesh_path(candidate):
            return _repair_w2_component_mesh_path(chunk, candidate)

    # Last parsed-property pass for chunks that encode the mesh indirectly.
    props = getattr(chunk, "PROPS", None) or []
    for prop in props:
        handles = getattr(prop, "Handles", None) or []
        for handle in handles:
            candidate = _mesh_path_from_handle(chunk, handle)
            if is_valid_mesh_path(candidate):
                return _repair_w2_component_mesh_path(chunk, candidate)
        candidate = _mesh_path_from_import_index(chunk, getattr(prop, "Index", None))
        if is_valid_mesh_path(candidate):
            return _repair_w2_component_mesh_path(chunk, candidate)
    return None

def _chunk_props_summary(chunk, limit=10):
    props = getattr(chunk, "PROPS", None) or []
    out = []
    for prop in props[:limit]:
        out.append(f"{getattr(prop, 'theName', '?')}:{getattr(prop, 'theType', '?')}")
    return ", ".join(out)

class JsonChunk(object):
    """docstring for JsonChunk."""
    def __init__(self):
        super(JsonChunk, self).__init__()
        self.chunkIndex = 0
        self.type = 0
        #![JsonIgnore]
        #self.refChunk = 0

    def __getitem__(self, item):
        return getattr(self, item)

    def __setitem__(self, item, value):
        setattr(self, item, value)

    def get(self, item, default=None):
        return getattr(self, item, default)

    def __contains__(self, item):
        return hasattr(self, item)

    def keys(self):
        return vars(self).keys()

    def items(self):
        return vars(self).items()

class ModelEnt(object):
    """docstring for ModelEnt."""
    def __init__(self, templateFilename, ns):
        super(ModelEnt, self).__init__()
        self.templateFilename = templateFilename
        self.ns = ns
        self.chunks = []
        #self.animation_face_object = False
    def __getitem__(self, item):
        return getattr(self, item)

    def __setitem__(self, item, value):
        setattr(self, item, value)

    def get(self, item, default=None):
        return getattr(self, item, default)

    def __contains__(self, item):
        return hasattr(self, item)

    def keys(self):
        return vars(self).keys()

    def items(self):
        return vars(self).items()

class CRigidMeshComponent(JsonChunk):
    """docstring for CRigidMeshComponent."""
    def __init__(self, *args, **kwargs):
        self.tags = None                   #" Type="TagList" />
        self.transform = None                   #" Type="EngineTransform" />
        self.transformParent = None                   #" Type="ptr:CHardAttachment" />
        self.guid = None                   #" Type="CGUID" />
        self.name = None                   #" Type="String" />
        self.isStreamed = None                   #" Type="Bool" />
        self.boundingBox = None                   #" Type="Box" />
        self.drawableFlags = None                   #" Type="EDrawableFlags" />
        self.lightChannels = None                   #" Type="ELightChannel" />
        self.renderingPlane = None                   #" Type="ERenderingPlane" />
        self.forceLODLevel = None                   #" Type="Int32" />
        self.forceAutoHideDistance = None                   #" Type="Uint16" />
        self.shadowImportanceBias = None                   #" Type="EMeshShadowImportanceBias" />
        self.defaultEffectParams = None                   #" Type="Vector" />
        self.defaultEffectColor = None                   #" Type="Color" />
        self.mesh = None                   #" Type="handle:CMesh" />
        self.pathLibCollisionType = None                   #" Type="EPathLibCollision" />
        self.fadeOnCameraCollision = None                   #" Type="Bool" />
        self.physicalCollisionType = None                   #" Type="CPhysicalCollision" />
        self.motionType = None                   #" Type="EMotionType" />
        self.linearDamping = None                   #" Type="Float" />
        self.angularDamping = None                   #" Type="Float" />
        self.linearVelocityClamp = None                   #" Type="Float" />
        w3_types.loadProps(self, args)
    
    def convert_for_io(self):
        self.transformParent = self.transformParent.Value-1 if self.transformParent else None
        self.mesh = _convert_mesh_value(self.mesh)
        self.transform = self.transform.EngineTransform if self.transform else None
        return self

class CMeshComponent(JsonChunk):
    """docstring for CMeshComponent."""
    def __init__(self, *args, **kwargs):
        #super(CMeshComponent, self).__init__()
        self.tags = None #Type="TagList"
        self.transform = None #Type="EngineTransform"
        self.transformParent = None #Type="ptr:CHardAttachment"
        self.guid = None #Type="CGUID"
        self.name = None #Type="String"
        self.isStreamed = None #Type="Bool"
        self.boundingBox = None #Type="Box"
        self.drawableFlags = None #Type="EDrawableFlags"
        self.lightChannels = None #Type="ELightChannel"
        self.renderingPlane = None #Type="ERenderingPlane"
        self.forceLODLevel = None #Type="Int32"
        self.forceAutoHideDistance = None #Type="Uint16"
        self.shadowImportanceBias = None #Type="EMeshShadowImportanceBias"
        self.defaultEffectParams = None #Type="Vector"
        self.defaultEffectColor = None #Type="Color"
        self.mesh = None #Type="handle:CMesh"
        w3_types.loadProps(self, args)
    
    def convert_for_io(self):
        self.transformParent = self.transformParent.Value-1 if self.transformParent else None
        self.mesh = _convert_mesh_value(self.mesh)
        self.transform = self.transform.EngineTransform if self.transform else None
        return self

class CCollisionShapeConvex(JsonChunk):
    def __init__(self, *args, **kwargs):
        self.physicalMaterialName = None
        self.vertices = None
        self.polygons = None
        w3_types.loadProps(self, args)
        if self.vertices and self.polygons:
            try:
                self.polygons = self.polygons.value
                self.vertices = [[prop.Value for prop in verts.MoreProps[:4]] for verts in self.vertices.More if hasattr(verts, 'MoreProps') and len(verts.MoreProps) >= 4]
            except Exception as e:
                log.error('Could not get CCollisionShapeConvex')
                

class CCollisionShapeTriMesh(JsonChunk):
    def __init__(self, *args, **kwargs):
        self.physicalMaterialNames = None
        self.vertices = None
        self.triangles = None
        self.physicalMaterialIndexes = None
        w3_types.loadProps(self, args)
        if self.vertices and self.triangles:
            try:
                raw_vertices = self.vertices
                if hasattr(raw_vertices, "More"):
                    self.vertices = [
                        [prop.Value for prop in verts.MoreProps[:4]]
                        for verts in raw_vertices.More
                        if hasattr(verts, 'MoreProps') and len(verts.MoreProps) >= 4
                    ]
            except Exception:
                log.error('Could not parse CCollisionShapeTriMesh.vertices')

            try:
                raw_triangles = self.triangles
                if hasattr(raw_triangles, "value"):
                    self.triangles = raw_triangles.value
            except Exception:
                log.error('Could not parse CCollisionShapeTriMesh.triangles')

            try:
                raw_names = self.physicalMaterialNames
                parsed_names = []
                if isinstance(raw_names, list):
                    parsed_names = [name for name in raw_names if isinstance(name, str) and name]
                elif raw_names is not None:
                    index_items = getattr(raw_names, "Index", None)
                    if isinstance(index_items, list):
                        for item in index_items:
                            name = None
                            if isinstance(item, str):
                                name = item
                            elif hasattr(item, "String"):
                                name = item.String
                            elif hasattr(item, "ToString"):
                                try:
                                    name = item.ToString()
                                except Exception:
                                    name = None
                            if isinstance(name, str) and name:
                                parsed_names.append(name)

                    if not parsed_names:
                        elements = getattr(raw_names, "elements", None)
                        if isinstance(elements, list):
                            for item in elements:
                                name = None
                                if isinstance(item, str):
                                    name = item
                                elif hasattr(item, "String"):
                                    name = item.String
                                elif hasattr(item, "value"):
                                    val = item.value
                                    if isinstance(val, str):
                                        name = val
                                    elif hasattr(val, "name") and hasattr(val.name, "value"):
                                        name = val.name.value
                                elif hasattr(item, "name") and hasattr(item.name, "value"):
                                    name = item.name.value
                                if isinstance(name, str) and name:
                                    parsed_names.append(name)

                if parsed_names or getattr(raw_names, "Count", None) == 0:
                    self.physicalMaterialNames = parsed_names
            except Exception:
                log.error('Could not parse CCollisionShapeTriMesh.physicalMaterialNames')

            try:
                raw_indexes = self.physicalMaterialIndexes
                parsed_indexes = None
                if isinstance(raw_indexes, list):
                    parsed_indexes = raw_indexes
                elif raw_indexes is not None:
                    if hasattr(raw_indexes, "value"):
                        parsed_indexes = raw_indexes.value
                    elif hasattr(raw_indexes, "More"):
                        parsed_indexes = [
                            entry.Value if hasattr(entry, "Value") else entry
                            for entry in raw_indexes.More
                        ]
                if parsed_indexes is not None:
                    self.physicalMaterialIndexes = parsed_indexes
            except Exception:
                log.error('Could not parse CCollisionShapeTriMesh.physicalMaterialIndexes')


class CCollisionShapeBox(JsonChunk): # ICollisionShape
    def __init__(self, *args, **kwargs):
        self.physicalMaterialName = None
        self.pose = None
        self.halfExtendsX = None
        self.halfExtendsY = None
        self.halfExtendsZ = None
        w3_types.loadProps(self, args)
        
        if self.pose:
            try:
                # self.pose.More is a list of 4 rows (X, Y, Z, Translation)
                # Each row has .More containing 4 items with .theName ('X','Y','Z','W') and .Value
                matrix_4x4 = []
                for row in self.pose.More:
                    row_values = []
                    for item in row.More:
                        row_values.append(float(item.Value))
                    matrix_4x4.append(row_values)
                
                # Store as a clean 4x4 list (row-major): [[rx, ry, rz, tx], [ux, uy, uz, ty], ...]
                self.matrix_world = matrix_4x4
                
            except Exception as e:
                log.error(f'Could not parse pose matrix for CCollisionShapeBox: {e}')
                self.matrix_world = None
        else:
            self.matrix_world = None
        
        
        # if self.pose:
        #     self.final_pose = []
        #     the_matrix = [
        #     ]
        #     try:
        #         for vec in self.pose.More: # Matrix
        #             the_vec = []
        #             for vec_item in vec.More: # vectors
        #                 the_vec.append({vec_item.theName : vec_item.Value})
        #             the_matrix.append(the_vec)
        #             self.final_pose.append({vec.theName : the_matrix})
        #     except Exception as e:
        #         log.error('Could not get CCollisionShapeBox')
        
class CCollisionShapeSphere(JsonChunk): # ICollisionShape
    def __init__(self, *args, **kwargs):
        self.physicalMaterialName = None
        self.radius = None
        self.pose = None
        w3_types.loadProps(self, args)
        
        if self.pose:
            try:
                # self.pose.More is a list of 4 rows (X, Y, Z, Translation)
                # Each row has .More containing 4 items with .theName ('X','Y','Z','W') and .Value
                matrix_4x4 = []
                for row in self.pose.More:
                    row_values = []
                    for item in row.More:
                        row_values.append(float(item.Value))
                    matrix_4x4.append(row_values)
                
                # Store as a clean 4x4 list (row-major): [[rx, ry, rz, tx], [ux, uy, uz, ty], ...]
                self.matrix_world = matrix_4x4
                
            except Exception as e:
                log.error(f'Could not parse pose matrix for CCollisionShapeBox: {e}')
                self.matrix_world = None
        else:
            self.matrix_world = None
        

class CCollisionShapeCapsule(JsonChunk): # ICollisionShape
    def __init__(self, *args, **kwargs):
        self.physicalMaterialName = None
        self.radius = None
        self.height = None
        self.pose = None
        w3_types.loadProps(self, args)
        
        if self.pose:
            try:
                # self.pose.More is a list of 4 rows (X, Y, Z, Translation)
                # Each row has .More containing 4 items with .theName ('X','Y','Z','W') and .Value
                matrix_4x4 = []
                for row in self.pose.More:
                    row_values = []
                    for item in row.More:
                        row_values.append(float(item.Value))
                    matrix_4x4.append(row_values)
                
                # Store as a clean 4x4 list (row-major): [[rx, ry, rz, tx], [ux, uy, uz, ty], ...]
                self.matrix_world = matrix_4x4
                
            except Exception as e:
                log.error(f'Could not parse pose matrix for CCollisionShapeBox: {e}')
                self.matrix_world = None
        else:
            self.matrix_world = None

class CStaticMeshComponent(CMeshComponent):
    """docstring for CStaticMeshComponent."""
    def __init__(self, *args, **kwargs):
        super(CStaticMeshComponent, self).__init__(*args, **kwargs)
        self.pathLibCollisionType = None #Type="EPathLibCollision"
        self.fadeOnCameraCollision = None #Type="Bool"
        self.physicalCollisionType = None #Type="CPhysicalCollision"

class CClothComponent(JsonChunk):
    """docstring for CClothComponent."""
    def __init__(self, resource):
        super(CClothComponent, self).__init__()
        self.resource = resource

class CMorphedMeshComponent(JsonChunk):
    """docstring for CMorphedMeshComponent."""
    def __init__(self, morphTarget:str, morphSource:str, morphComponentId:str):
        super(CMorphedMeshComponent, self).__init__()
        self.morphTarget = morphTarget
        self.morphSource = morphSource
        #self.morphControlTextures = morphSource
        self.morphComponentId = morphComponentId

class CMimicComponent(JsonChunk):
    """docstring for CMimicComponent."""
    def __init__(self, name:str, mimicFace:str):
        super(CMimicComponent, self).__init__()
        self.name = name
        self.mimicFace = mimicFace

class CAnimatedComponent(JsonChunk):
    """docstring for CAnimatedComponent."""
    def __init__(self, *args, name:str = "", skeleton:str = ""):
        super(CAnimatedComponent, self).__init__()
        self.transform = None #Type="EngineTransform"
        self.transformParent = None #Type="ptr:CHardAttachment"
        self.guid = None #Type="CGUID"
        self.name = name
        self.skeleton = skeleton
        if args:
            w3_types.loadProps(self, args)

    def convert_for_io(self):
        self.transformParent = self.transformParent.Value-1 if self.transformParent else None
        self.transform = self.transform.EngineTransform if self.transform else None
        return self

class CAnimDangleComponent(JsonChunk):
    """docstring for CAnimDangleComponent."""
    def __init__(self, name:str, constraint:int):
        super(CAnimDangleComponent, self).__init__()
        self.name = name
        self.constraint = constraint

class CAnimDangleBufferComponent(JsonChunk):
    """docstring for CAnimDangleBufferComponent."""
    def __init__(self, name:str, skeleton:str):
        super(CAnimDangleBufferComponent, self).__init__()
        self.name = name
        self.skeleton = skeleton

class SkinningAttachment(JsonChunk):
    """docstring for SkinningAttachment."""
    def __init__(self, parent:int, child:int):
        super(SkinningAttachment, self).__init__()
        self.parent = parent
        self.child = child

class CMeshSkinningAttachment(SkinningAttachment):
    """docstring for CMeshSkinningAttachment."""
    def __init__(self, parent:int, child:int):
        super(CMeshSkinningAttachment, self).__init__(parent, child)

class CAnimatedAttachment(SkinningAttachment):
    """docstring for CAnimatedAttachment."""
    def __init__(self, parent:int, child:int):
        super(CAnimatedAttachment, self).__init__(parent, child)

class CHardAttachment(SkinningAttachment):
    """docstring for CHardAttachment."""
    def __init__(self, *args, **kwargs): #parent:int , child:int , parentSlot:int , parentSlotName:str):
        #super(CHardAttachment, self).__init__(parent, child)
        self.parent = None # Type="ptr:CNode"
        self.child = None # Type="ptr:CNode"
        self.isBroken:bool = None # Type="Bool"
        self.relativeTransform = None # Type="EngineTransform"
        self.parentSlotName = None # Type="CName"
        self.attachmentFlags = None # Type="EHardAttachmentFlags"
        self.parentSlot = None # Type="ptr:ISlot"
        w3_types.loadProps(self, args)
    
    def convert_for_io(self):
        self.parent = self.parent.Value-1 if self.parent else None
        self.child = self.child.Value-1 if self.child else None
        self.parentSlot = self.parentSlot.Value-1 if self.parentSlot else None
        self.relativeTransform = self.relativeTransform.EngineTransform if self.relativeTransform else None
        return self


class CAnimDangleConstraint_Breast(JsonChunk):
    """docstring for CAnimDangleConstraint_Breast."""
    def __init__(self, skeleton):
        super(CAnimDangleConstraint_Breast, self).__init__()
        self.skeleton = skeleton

class CAnimDangleConstraint_Collar(JsonChunk):
    """docstring for CAnimDangleConstraint_Collar."""
    def __init__(self, skeleton):
        super(CAnimDangleConstraint_Collar, self).__init__()
        self.skeleton = skeleton

class CAnimDangleConstraint_Pusher(JsonChunk):
    """docstring for CAnimDangleConstraint_Pusher."""
    def __init__(self, skeleton):
        super(CAnimDangleConstraint_Pusher, self).__init__()
        self.skeleton = skeleton

class CAnimDangleConstraint_Hinge(JsonChunk):
    """docstring for CAnimDangleConstraint_Hinge."""
    def __init__(self, skeleton):
        super(CAnimDangleConstraint_Hinge, self).__init__()
        self.skeleton = skeleton

class CAnimDangleConstraint_Hood(JsonChunk):
    """docstring for CAnimDangleConstraint_Hood."""
    def __init__(self, skeleton):
        super(CAnimDangleConstraint_Hood, self).__init__()
        self.skeleton = skeleton

class CAnimDangleConstraint_Dress(JsonChunk):
    """docstring for CAnimDangleConstraint_Dress."""
    def __init__(self, skeleton):
        super(CAnimDangleConstraint_Dress, self).__init__()
        self.skeleton = skeleton

class CAnimDangleConstraint_Dyng(JsonChunk):
    """docstring for CAnimDangleConstraint_Dyng."""
    def __init__(self, skeleton, dyng):
        super(CAnimDangleConstraint_Dyng, self).__init__()
        self.skeleton = skeleton
        self.dyng = dyng

class CSkeletonBoneSlot(JsonChunk):
    """docstring for CSkeletonBoneSlot."""
    def __init__(self, boneIndex:int):
        super(CSkeletonBoneSlot, self).__init__()
        self.boneIndex = boneIndex

class CCameraComponent(JsonChunk):
    def __init__(self, name):
        super(CCameraComponent, self).__init__()
        self.name = name
        self.transformParent = None #<ptr:CHardAttachment>

class CPointLightComponent(JsonChunk):
    def __init__(self, *args, **kwargs):
        super(CPointLightComponent, self).__init__()
        self.transform = None
        self.transformParent = None
        self.name = None
        self.radius = None
        self.color = None
        self.brightness = None
        w3_types.loadProps(self, args)

    def convert_for_io(self):
        self.transformParent = self.transformParent.Value-1 if self.transformParent else None
        self.transform = self.transform.EngineTransform if self.transform else None
        self.color = _convert_color_value(self.color)
        return self

class CSpotLightComponent(CPointLightComponent):
    def __init__(self, *args, **kwargs):
        super(CSpotLightComponent, self).__init__(*args, **kwargs)
        self.innerAngle = None
        self.outerAngle = None
        self.shadowCastingMode = None
        self.shadowFadeDistance = None
        self.lightFlickering = None
        w3_types.loadProps(self, args)

entity_type_dict = {
    "CMeshComponent": CMeshComponent,
    "CClothComponent": CClothComponent,
    "CFurComponent": CMeshComponent,
    "CMorphedMeshComponent": CMorphedMeshComponent,
    "CMimicComponent": CMimicComponent,
    "CMeshSkinningAttachment": CMeshSkinningAttachment,
    "CAnimatedAttachment": CAnimatedAttachment,
    "CAnimDangleBufferComponent": CAnimDangleBufferComponent,
    "CAnimDangleComponent": CAnimDangleComponent,
    "CStaticMeshComponent": CStaticMeshComponent,
    "CAnimatedComponent": CAnimatedComponent,
    "CHardAttachment": CHardAttachment,
    "CSkeletonBoneSlot": CSkeletonBoneSlot,
    "CCameraComponent": CCameraComponent,
    "CPointLightComponent": CPointLightComponent,
    "CSpotLightComponent": CSpotLightComponent,
}

CAnimDangleConstraint_types = {
    "CAnimDangleConstraint_Dyng": CAnimDangleConstraint_Dyng,
    "CAnimDangleConstraint_Breast": CAnimDangleConstraint_Breast,
    "CAnimDangleConstraint_Collar": CAnimDangleConstraint_Collar,
    "CAnimDangleConstraint_Dress": CAnimDangleConstraint_Dress,
    "CAnimDangleConstraint_Hood": CAnimDangleConstraint_Hood,
    "CAnimDangleConstraint_Hinge": CAnimDangleConstraint_Hinge,
    "CAnimDangleConstraint_Pusher": CAnimDangleConstraint_Pusher,
}


def _mesh_chunk_signature(chunk):
    return (
        getattr(chunk, "type", None),
        getattr(chunk, "name", None),
        getattr(chunk, "mesh", None),
        getattr(chunk, "resource", None),
        getattr(chunk, "mimicFace", None),
        getattr(chunk, "guid", None),
        getattr(chunk, "transformParent", None),
        _transform_signature(getattr(chunk, "transform", None)),
    )

def _transform_signature(transform):
    if not transform:
        return None
    keys = ("X", "Y", "Z", "Yaw", "Pitch", "Roll", "Scale_x", "Scale_y", "Scale_z")
    if isinstance(transform, dict):
        return tuple(transform.get(key) for key in keys)
    return tuple(getattr(transform, key, None) for key in keys)

def _color_signature(color):
    if not color:
        return None
    if isinstance(color, dict):
        return tuple(color.get(key) for key in ("Red", "Green", "Blue", "Alpha"))
    return tuple(getattr(color, key, None) for key in ("Red", "Green", "Blue", "Alpha"))

def _light_chunk_signature(chunk):
    return (
        getattr(chunk, "type", None),
        getattr(chunk, "name", None),
        getattr(chunk, "brightness", None),
        getattr(chunk, "radius", None),
        getattr(chunk, "innerAngle", None),
        getattr(chunk, "outerAngle", None),
        _color_signature(getattr(chunk, "color", None)),
        _transform_signature(getattr(chunk, "transform", None)),
    )

def _animated_chunk_signature(chunk):
    return (
        getattr(chunk, "type", None),
        getattr(chunk, "name", None),
        getattr(chunk, "skeleton", None),
        tuple(getattr(chunk, "animationSets", None) or []),
        getattr(chunk, "transformParent", None),
        _transform_signature(getattr(chunk, "transform", None)),
    )


def _extract_cname_array_values(prop):
    out = []
    seen = set()
    for item in getattr(prop, "Index", None) or []:
        value = None
        if hasattr(item, "String"):
            value = item.String
        elif hasattr(item, "value"):
            value = item.value
        elif hasattr(item, "name") and hasattr(item.name, "value"):
            value = item.name.value
        elif hasattr(item, "ToString"):
            try:
                value = item.ToString()
            except Exception:
                value = None
        if hasattr(value, "value"):
            value = value.value
        if not isinstance(value, str):
            continue
        value = value.strip()
        if not value or value == "CName":
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def _find_prop_by_name(container, prop_name: str):
    if not container:
        return None
    getter = getattr(container, "GetVariableByName", None)
    if callable(getter):
        try:
            prop = getter(prop_name)
        except Exception:
            prop = None
        if prop:
            return prop
    for attr_name in ("MoreProps", "More", "PROPS"):
        for prop in getattr(container, attr_name, None) or []:
            if getattr(prop, "theName", None) == prop_name:
                return prop
    return None


def _iter_struct_items(prop):
    items = list(getattr(prop, "More", None) or [])
    if not items:
        return []
    if getattr(prop, "Count", None) == 1 and all(getattr(item, "theName", None) for item in items):
        return [prop]
    return items


def _find_chunk_by_name(chunks, name, chunk_type=None):
    wanted = str(name or "").strip().lower()
    if not wanted:
        return None
    secondary_match = None
    for chunk in chunks or []:
        if chunk_type and getattr(chunk, "Type", None) != chunk_type:
            continue
        chunk_name = _prop_to_string(_find_prop_by_name(chunk, "name"))
        if not chunk_name:
            continue
        chunk_key = chunk_name.strip().lower()
        if chunk_key == wanted:
            return chunk
        if secondary_match is None and chunk_key.lstrip("_") == wanted.lstrip("_"):
            secondary_match = chunk
    return secondary_match


def _iter_w2_body_parts(template_chunk):
    body_parts_prop = _find_prop_by_name(template_chunk, "bodyParts")
    if not body_parts_prop:
        return
    for body_part in _iter_struct_items(body_parts_prop):
        part_name = _prop_to_string(_find_prop_by_name(body_part, "name"))
        if part_name:
            yield part_name, body_part


def _iter_w2_body_part_states(body_part_element):
    states_prop = _find_prop_by_name(body_part_element, "states")
    if not states_prop:
        return
    for state in _iter_struct_items(states_prop):
        yield _prop_to_string(_find_prop_by_name(state, "name")), state


def _iter_w2_component_refs(state_property_or_element):
    components_prop = _find_prop_by_name(state_property_or_element, "componentsInUse")
    if not components_prop:
        return
    for component_ref in _iter_struct_items(components_prop):
        component_name = _prop_to_string(_find_prop_by_name(component_ref, "name"))
        class_name = _prop_to_string(_find_prop_by_name(component_ref, "className"))
        if component_name:
            yield component_name, class_name


def _handle_ref_chunk(chunk, handle):
    if not chunk or not handle or not getattr(handle, "ChunkHandle", False):
        return None
    cr2w_file = getattr(chunk, "_W_CLASS__CR2WFILE", None)
    ref_idx = getattr(handle, "Reference", None)
    if not cr2w_file or not isinstance(ref_idx, int):
        return None
    if 0 <= ref_idx < len(cr2w_file.CHUNKS.CHUNKS):
        return cr2w_file.CHUNKS.CHUNKS[ref_idx]
    return None


def _convert_chunk_for_model(chunk):
    if not chunk:
        return None
    if chunk.Type in ("CMeshComponent", "CStaticMeshComponent", "CFurComponent", "CRigidMeshComponent", "CRagdollMeshComponent"):
        component = CMeshComponent(chunk).convert_for_io()
        component.mesh = _resolve_mesh_path(chunk, getattr(component, "mesh", None))
        return component if component.mesh else None
    if chunk.Type == "CClothComponent":
        resource = _resolve_repo_path(chunk, "resource", ".redcloth")
        return CClothComponent(resource) if resource else None
    if chunk.Type == "CMorphedMeshComponent":
        morph_target = _resolve_repo_path(chunk, "morphTarget", ".w2mesh")
        morph_source = _resolve_repo_path(chunk, "morphSource", ".w2mesh")
        morph_component_id = _prop_to_string(_find_prop_by_name(chunk, "morphComponentId"))
        return CMorphedMeshComponent(morph_target, morph_source, morph_component_id)
    return None


def _make_mesh_proxy_chunk(source_chunk, name: str, mesh_path: str, skeleton: str | None = None):
    proxy = JsonChunk()
    proxy.type = "CMeshComponent"
    proxy.chunkIndex = getattr(source_chunk, "ChunkIndex", 0)
    proxy.name = name
    proxy.mesh = mesh_path
    proxy.skeleton = skeleton
    return proxy


def _resolve_w2_body_part_chunks(template_chunk, part_names, chunks):
    wanted_parts = {str(part or "").strip().lower() for part in part_names if part}
    if not wanted_parts:
        return []

    candidate_templates = []
    seen_templates = set()

    def _add_template(candidate):
        if not candidate or getattr(candidate, "Type", None) != "CEntityTemplate":
            return
        marker = id(candidate)
        if marker in seen_templates:
            return
        seen_templates.add(marker)
        candidate_templates.append(candidate)

    _add_template(template_chunk)
    for candidate in chunks or []:
        if _find_prop_by_name(candidate, "bodyParts"):
            _add_template(candidate)

    body_parts = {}
    for candidate in candidate_templates:
        for part_name, body_part in _iter_w2_body_parts(candidate):
            part_key = part_name.lower()
            body_parts[part_key] = body_part
            body_parts.setdefault(part_key.lstrip("_"), body_part)

    resolved_chunks = []
    seen = set()
    for part_name in wanted_parts:
        body_part = body_parts.get(part_name) or body_parts.get(part_name.lstrip("_"))
        if not body_part:
            continue

        states = list(_iter_w2_body_part_states(body_part))
        preferred_states = [state for state in states if str(state[0] or "").lower() == "default"] or states[:1]
        for _, state in preferred_states:
            for component_name, class_name in _iter_w2_component_refs(state):
                ref_chunk = _find_chunk_by_name(chunks, component_name, class_name)
                if not ref_chunk:
                    continue
                signature = (getattr(ref_chunk, "Type", None), getattr(ref_chunk, "ChunkIndex", None))
                if signature in seen:
                    continue
                seen.add(signature)
                resolved_chunks.append(ref_chunk)
    return resolved_chunks


def _collect_w2_body_part_chunk_indices(template_chunk, chunks):
    chunk_indices = set()
    for _, body_part in _iter_w2_body_parts(template_chunk):
        for _, state in _iter_w2_body_part_states(body_part):
            for component_name, class_name in _iter_w2_component_refs(state):
                ref_chunk = _find_chunk_by_name(chunks, component_name, class_name)
                if not ref_chunk:
                    continue
                chunk_index = getattr(ref_chunk, "ChunkIndex", None)
                if isinstance(chunk_index, int) and chunk_index > 0:
                    chunk_indices.add(chunk_index)
    return chunk_indices


def _collect_w2_body_part_component_names(template_chunk):
    names = set()
    for _, body_part in _iter_w2_body_parts(template_chunk):
        for _, state in _iter_w2_body_part_states(body_part):
            for component_name, _class_name in _iter_w2_component_refs(state):
                if component_name:
                    names.add(str(component_name).strip().lower())
    return names


def _build_w2_head_chunks(chunks, head_name):
    if not head_name:
        return []
    head_chunk = _find_chunk_by_name(chunks, head_name, "CHeadDefinifion")
    if not head_chunk:
        return []

    head_mesh_prop = _find_prop_by_name(head_chunk, "meshesForBaseHead")
    if not head_mesh_prop:
        return []

    head_chunks = []
    seen = set()
    for handle in getattr(head_mesh_prop, "Handles", None) or []:
        mesh_path = _resolve_handle_repo_path(head_chunk, handle, ".w2mesh")
        if not mesh_path:
            continue
        mesh_key = _repo_path_key(mesh_path)
        if mesh_key in seen:
            continue
        seen.add(mesh_key)
        source_chunk = _handle_ref_chunk(head_chunk, handle) or head_chunk
        # poseSkeleton in cooked W2 head definitions often points to an embedded
        # mimic/face skeleton, not a mesh skinning rig. Do not coerce it into an
        # external .w2rig path for normal mesh import.
        head_chunks.append(_make_mesh_proxy_chunk(source_chunk, head_name, mesh_path, None))
    return head_chunks


def _build_w2_cooked_appearance_template(file, template_chunk, appearance, current_app, chunks, base_mesh_paths):
    template_filename = f"{Path(file.fileName).stem}:{current_app.name}:cooked_bodyparts"
    model_ent = ModelEnt(template_filename, current_app.name)
    seen_mesh_paths = set(base_mesh_paths or [])
    seen_signatures = set()

    parts_prop = _find_prop_by_name(appearance, "parts")
    part_names = _extract_cname_array_values(parts_prop) if parts_prop else []
    for source_chunk in _resolve_w2_body_part_chunks(template_chunk, part_names, chunks):
        converted_chunk = _convert_chunk_for_model(source_chunk)
        if not converted_chunk:
            continue
        mesh_path = getattr(converted_chunk, "mesh", None)
        if mesh_path:
            mesh_key = _repo_path_key(mesh_path)
            if mesh_key in seen_mesh_paths:
                continue
            seen_mesh_paths.add(mesh_key)
        converted_chunk.type = source_chunk.Type
        converted_chunk.chunkIndex = source_chunk.ChunkIndex
        chunk_signature = _mesh_chunk_signature(converted_chunk)
        if chunk_signature in seen_signatures:
            continue
        seen_signatures.add(chunk_signature)
        model_ent.chunks.append(converted_chunk)

    head_name = _prop_to_string(_find_prop_by_name(appearance, "headName")) or getattr(current_app, "headName", None)
    for head_chunk in _build_w2_head_chunks(chunks, head_name):
        mesh_path = getattr(head_chunk, "mesh", None)
        if mesh_path:
            mesh_key = _repo_path_key(mesh_path)
            if mesh_key in seen_mesh_paths:
                continue
            seen_mesh_paths.add(mesh_key)
        chunk_signature = _mesh_chunk_signature(head_chunk)
        if chunk_signature in seen_signatures:
            continue
        seen_signatures.add(chunk_signature)
        model_ent.chunks.append(head_chunk)

    return model_ent if model_ent.chunks else None

def chunk_append(new_mesh, chunk, item, added_chunks=None):
    new_mesh.chunks.append(item)
    new_mesh.chunks[-1].type = chunk.Type
    new_mesh.chunks[-1].chunkIndex = chunk.ChunkIndex
    if added_chunks is not None:
        added_chunks.add(chunk.ChunkIndex)

# Structural/metadata chunk types handled elsewhere (create_CEntity, w3_material, etc.)
_KNOWN_STRUCTURAL_CHUNKS = {
    'CWetnessComponent',
    'CItemEntity', # Handled separately in ReadTemplate (streaming buffer path).
    "CEntityTemplate",
    "CEntity",
    "CGameplayEntity",
    "CActor",
    "CNewNPC",
    "CR4Player",
    "W3PlayerWitcher",
    "W3ReplacerCiri",
    "CNormalBlendComponent",
    "CNormalBlendAttachment",
    "CMaterialInstance",
    "CMovingPhysicalAgentComponent",
    "CExternalProxyComponent",
    "CDropPhysicsSetup",
}

_STREAMED_ITEM_CHUNK_TYPES = {
    "CItemEntity",
    "CWitcherSword",
    "Crossbow",
    "CWitcherJacket",
    "CWitcherPants",
    "CWitcherBoots",
}

def ReadTemplate(CR2W_FILE, new_mesh, this_Entity = None) -> ModelEnt:
    previous_chunk = False
    CHUNKS = CR2W_FILE.CHUNKS.CHUNKS
    mesh_import_paths = _collect_mesh_import_paths(CR2W_FILE)
    mesh_import_cursor = 0
    streamed_component_cache = {"resolved": False, "chunk": None}
    seen_mesh_signatures = set()
    seen_light_signatures = set()
    seen_animated_signatures = set()

    def _next_mesh_import_path():
        nonlocal mesh_import_cursor
        if mesh_import_cursor < len(mesh_import_paths):
            path = mesh_import_paths[mesh_import_cursor]
            mesh_import_cursor += 1
            return path
        return None

    def _append_unique_chunk(source_chunk, converted_chunk, added_chunks=None):
        converted_chunk.type = getattr(source_chunk, "Type", getattr(converted_chunk, "type", None))
        converted_chunk.chunkIndex = getattr(source_chunk, "ChunkIndex", getattr(converted_chunk, "chunkIndex", 0))
        signature = _mesh_chunk_signature(converted_chunk)
        if signature in seen_mesh_signatures:
            return False
        seen_mesh_signatures.add(signature)
        chunk_append(new_mesh, source_chunk, converted_chunk, added_chunks)
        return True

    def _append_unique_light_chunk(source_chunk, converted_chunk, added_chunks=None):
        converted_chunk.type = getattr(source_chunk, "Type", getattr(converted_chunk, "type", None))
        converted_chunk.chunkIndex = getattr(source_chunk, "ChunkIndex", getattr(converted_chunk, "chunkIndex", 0))
        signature = _light_chunk_signature(converted_chunk)
        if signature in seen_light_signatures:
            return False
        seen_light_signatures.add(signature)
        chunk_append(new_mesh, source_chunk, converted_chunk, added_chunks)
        return True

    def _append_unique_animated_chunk(source_chunk, converted_chunk, added_chunks=None):
        converted_chunk.type = getattr(source_chunk, "Type", getattr(converted_chunk, "type", None))
        converted_chunk.chunkIndex = getattr(source_chunk, "ChunkIndex", getattr(converted_chunk, "chunkIndex", 0))
        signature = _animated_chunk_signature(converted_chunk)
        if signature in seen_animated_signatures:
            return False
        seen_animated_signatures.add(signature)
        chunk_append(new_mesh, source_chunk, converted_chunk, added_chunks)
        return True

    def _get_streamed_component_chunk():
        if streamed_component_cache["resolved"]:
            return streamed_component_cache["chunk"]
        streamed_component_cache["resolved"] = True
        try:
            level_data = create_level(CR2W_FILE, "")
        except Exception:
            streamed_component_cache["chunk"] = None
            return None
        entities = getattr(level_data, "Entities", None)
        if not entities:
            streamed_component_cache["chunk"] = None
            return None
        stream_buf = getattr(entities[0], "streamingDataBuffer", None)
        if not (
            stream_buf
            and hasattr(stream_buf, "CHUNKS")
            and getattr(stream_buf.CHUNKS, "CHUNKS", None)
        ):
            streamed_component_cache["chunk"] = None
            return None
        streamed_component_cache["chunk"] = stream_buf.CHUNKS.CHUNKS[0]
        return streamed_component_cache["chunk"]

    def _append_streamed_mesh(owner_chunk, streamed_chunk):
        if not streamed_chunk:
            return False
        streamed_type = getattr(streamed_chunk, "Type", "")
        try:
            if streamed_type == "CRigidMeshComponent":
                streamed_component = CRigidMeshComponent(streamed_chunk).convert_for_io()
            else:
                # CItemEntity/Crossbow buffers can expose CMeshComponent-like chunks.
                streamed_component = CMeshComponent(streamed_chunk).convert_for_io()
        except Exception as e:
            log.warning(
                "Failed to convert streamed mesh chunk for %s #%s (%s): %s",
                owner_chunk.Type,
                owner_chunk.ChunkIndex,
                streamed_type or "unknown",
                e,
            )
            return False

        streamed_component.mesh = _resolve_mesh_path(streamed_chunk, streamed_component.mesh)
        if not streamed_component.mesh:
            streamed_component.mesh = _next_mesh_import_path()
        if streamed_component.mesh:
            appended = _append_unique_chunk(owner_chunk, streamed_component)
            if appended:
                new_mesh.chunks[-1].type = streamed_type or new_mesh.chunks[-1].type
            return appended

        log.warning(
            f"Skipping {owner_chunk.Type} with invalid streamed mesh ref: {owner_chunk.ChunkIndex}; "
            f"props={_chunk_props_summary(streamed_chunk)}"
        )
        return False
    
    for chunk in CHUNKS:
        if (chunk.Type == "CMeshComponent"):
            mesh_component = CMeshComponent(chunk).convert_for_io()
            mesh_component.mesh = _resolve_mesh_path(chunk, mesh_component.mesh)
            if not mesh_component.mesh:
                mesh_component.mesh = _next_mesh_import_path()
            if mesh_component.mesh:
                _append_unique_chunk(chunk, mesh_component)
            else:
                log.warning(
                    f"Skipping CMeshComponent with invalid mesh ref in template: {chunk.ChunkIndex}; "
                    f"props={_chunk_props_summary(chunk)}"
                )
        elif (chunk.Type == "CRigidMeshComponent" or chunk.Type == "CRagdollMeshComponent"):
            mesh_component = CMeshComponent(chunk).convert_for_io()
            mesh_component.mesh = _resolve_mesh_path(chunk, mesh_component.mesh)
            if not mesh_component.mesh:
                mesh_component.mesh = _next_mesh_import_path()
            if mesh_component.mesh:
                _append_unique_chunk(chunk, mesh_component)
            else:
                log.warning(
                    f"Skipping {chunk.Type} with invalid mesh ref in template: {chunk.ChunkIndex}; "
                    f"props={_chunk_props_summary(chunk)}"
                )
        elif chunk.Type in _STREAMED_ITEM_CHUNK_TYPES:
            # Inventory items across W2/W3 can use inline streamed mesh buffers,
            # but some templates still expose regular mesh components instead.
            if not _append_streamed_mesh(chunk, _get_streamed_component_chunk()):
                log.debug(
                    "%s has no streamingDataBuffer in template chunk %s; "
                    "falling back to regular mesh components.",
                    chunk.Type,
                    chunk.ChunkIndex,
                )
        elif (chunk.Type == "CClothComponent"):
            if chunk.GetVariableByName("resource"): #! sometimes there are no resource in files??
                cloth = chunk.GetVariableByName("resource").ToString()
                chunk_append(new_mesh, chunk, CClothComponent(cloth))
        elif (chunk.Type == "CFurComponent"):
            if (chunk.GetVariableByName("mesh")):
                fur_component = CMeshComponent(chunk).convert_for_io()
                fur_component.mesh = _resolve_mesh_path(chunk, fur_component.mesh)
                if not fur_component.mesh:
                    fur_component.mesh = _next_mesh_import_path()
                if fur_component.mesh:
                    _append_unique_chunk(chunk, fur_component)
                else:
                    log.warning(
                        f"Skipping CFurComponent with invalid mesh ref in template: {chunk.ChunkIndex}; "
                        f"props={_chunk_props_summary(chunk)}"
                    )
        elif (chunk.Type == "CMorphedMeshComponent"):
            morphTarget = _resolve_repo_path(chunk, "morphTarget", ".w2mesh")
            morphSource = _resolve_repo_path(chunk, "morphSource", ".w2mesh")
            morphComponentId = chunk.GetVariableByName("morphComponentId").ToString()
            chunk_append(new_mesh, chunk, CMorphedMeshComponent(morphTarget, morphSource, morphComponentId))
        elif (chunk.Type == "CMimicComponent"):
            name = chunk.GetVariableByName("name").ToString()
            mimicFace = (
                _resolve_repo_path(chunk, "mimicFace", ".w3fac")
                or _resolve_repo_path(chunk, "mimicFace", ".w2fac")
            )
            chunk_append(new_mesh, chunk, CMimicComponent(name, mimicFace))
            #TODO GetFACE needed?
            #new_mesh.animation_face_object = GetFace(mimicFace)
        elif (chunk.Type == "CMeshSkinningAttachment"):
            parent = chunk.GetVariableByName("parent").Value-1
            child = chunk.GetVariableByName("child").Value-1
            chunk_append(new_mesh, chunk, CMeshSkinningAttachment(parent, child))
        elif (chunk.Type == "CAnimatedAttachment"):
            parent = chunk.GetVariableByName("parent").Value-1
            child = chunk.GetVariableByName("child").Value-1
            chunk_append(new_mesh, chunk, CAnimatedAttachment(parent, child))
        elif (chunk.Type == "CAnimDangleBufferComponent"):
            name = chunk.GetVariableByName("name").ToString()
            skeleton = _resolve_repo_path(chunk, "skeleton", ".w2rig")
            chunk_append(new_mesh, chunk, CAnimDangleBufferComponent(name, skeleton))
        elif (chunk.Type == "CAnimDangleComponent"):
            name = chunk.GetVariableByName("name").ToString()
            constraint_var = chunk.GetVariableByName("constraint")
            constraint = constraint_var.Value - 1 if constraint_var else None
            chunk_append(new_mesh, chunk, CAnimDangleComponent(name, constraint))
        elif (chunk.Type == "CAnimDangleConstraint_Dyng"):
            dyng = _resolve_repo_path(chunk, "dyng", ".w3dyng") if chunk.GetVariableByName("dyng") else None
            if not dyng and chunk.GetVariableByName("dyng"):
                dyng = _resolve_repo_path(chunk, "dyng", ".dyng")
            skeleton = _resolve_repo_path(chunk, "skeleton", ".w2rig") if chunk.GetVariableByName("skeleton") else None
            chunk_append(new_mesh, chunk, CAnimDangleConstraint_Dyng(skeleton, dyng))
        elif (chunk.Type in CAnimDangleConstraint_types):
            skeleton = _resolve_repo_path(chunk, "skeleton", ".w2rig")
            chunk_append(new_mesh, chunk, CAnimDangleConstraint_types[chunk.Type](skeleton))
        elif (chunk.Type == "CHardAttachment"): #TODO NormalBlend Stuff
            if (chunk.GetVariableByName("parentSlot")):
                chunk_append(new_mesh, chunk, CHardAttachment(chunk).convert_for_io())
        else:
            if chunk.Type in _KNOWN_STRUCTURAL_CHUNKS:
                log.debug("Skipping structural chunk in ReadTemplate: %s", chunk.Type)
            else:
                log.warning("Unknown Character Chunk: %s", chunk.Type)
    return new_mesh, this_Entity

def LoadCEntityTemplateFile(templateFilename: str) -> ModelEnt:
    cache_key = _template_cache_key(templateFilename)
    if cache_key in _template_file_cache:
        return copy.deepcopy(_template_file_cache[cache_key])

    new_mesh = ModelEnt(templateFilename, Path(templateFilename).stem)
    if os.path.isabs(templateFilename) and os.path.exists(templateFilename):
        fileNameFull = templateFilename
    else:
        fileNameFull = repo_file(templateFilename)
    cr2w_file = read_CR2W(fileNameFull)
    parsed_mesh, parsed_entity = ReadTemplate(cr2w_file, new_mesh)
    has_mesh = any(getattr(c, "mesh", None) for c in getattr(parsed_mesh, "chunks", []))
    if has_mesh:
        _template_file_cache[cache_key] = (parsed_mesh, parsed_entity)
        return copy.deepcopy(_template_file_cache[cache_key])

    full_entity = create_CEntity(cr2w_file)
    full_mesh = getattr(full_entity, "staticMeshes", None)
    if full_mesh and getattr(full_mesh, "chunks", None):
        has_full_mesh = any(getattr(c, "mesh", None) for c in full_mesh.chunks)
        if has_full_mesh:
            full_mesh.templateFilename = templateFilename
            full_mesh.ns = Path(templateFilename).stem
            _template_file_cache[cache_key] = (full_mesh, full_entity)
            return copy.deepcopy(_template_file_cache[cache_key])
    _template_file_cache[cache_key] = (parsed_mesh, parsed_entity)
    return copy.deepcopy(_template_file_cache[cache_key])

def create_CEntity(file, _inherit_visited=None):
    hasCMovingPhysicalAgentComponent = False
    CHUNKS = file.CHUNKS.CHUNKS
    this_Entity = w3_types.Entity()
    this_Entity.name = Path(file.fileName).stem
    this_Entity.appearances = []
    this_Entity.coloringEntries = []
    this_Entity.slots = []
    new_mesh = ModelEnt("staticMeshes", "staticMeshes")
    added_chunks = set()  # Track chunk indices already added to avoid duplicates
    seen_streamed_mesh_paths = set()  # Track mesh paths already added via streamingDataBuffer to avoid duplicates
    seen_mesh_signatures = set()
    seen_light_signatures = set()
    seen_animated_signatures = set()
    this_Entity.CAnimAnimsetsParam = []
    this_Entity.CAnimMimicParam = []
    mesh_import_paths = _collect_mesh_import_paths(file)
    mesh_import_cursor = 0
    top_level_template_includes = []
    top_level_template_include_set = set()
    pending_w2_appearances = []
    w2_body_part_chunk_indices = set()
    w2_body_part_component_names = set()
    w2_related_entity_paths = []
    w2_related_files = []
    w2_related_search_chunks = []
    inherit_visited = set(_inherit_visited or [])
    current_file_name = getattr(file, "fileName", None)
    if current_file_name:
        inherit_visited.add(os.path.normcase(os.path.normpath(str(current_file_name))))

    def _next_mesh_import_path():
        nonlocal mesh_import_cursor
        if mesh_import_cursor < len(mesh_import_paths):
            path = mesh_import_paths[mesh_import_cursor]
            mesh_import_cursor += 1
            return path
        return None

    def _append_unique_chunk(source_chunk, converted_chunk, added_chunks=None):
        converted_chunk.type = getattr(source_chunk, "Type", getattr(converted_chunk, "type", None))
        converted_chunk.chunkIndex = getattr(source_chunk, "ChunkIndex", getattr(converted_chunk, "chunkIndex", 0))
        signature = _mesh_chunk_signature(converted_chunk)
        if signature in seen_mesh_signatures:
            return False
        seen_mesh_signatures.add(signature)
        chunk_append(new_mesh, source_chunk, converted_chunk, added_chunks)
        return True

    def _append_unique_light_chunk(source_chunk, converted_chunk, added_chunks=None):
        converted_chunk.type = getattr(source_chunk, "Type", getattr(converted_chunk, "type", None))
        converted_chunk.chunkIndex = getattr(source_chunk, "ChunkIndex", getattr(converted_chunk, "chunkIndex", 0))
        signature = _light_chunk_signature(converted_chunk)
        if signature in seen_light_signatures:
            return False
        seen_light_signatures.add(signature)
        chunk_append(new_mesh, source_chunk, converted_chunk, added_chunks)
        return True

    def _append_unique_animated_chunk(source_chunk, converted_chunk, added_chunks=None):
        converted_chunk.type = getattr(source_chunk, "Type", getattr(converted_chunk, "type", None))
        converted_chunk.chunkIndex = getattr(source_chunk, "ChunkIndex", getattr(converted_chunk, "chunkIndex", 0))
        signature = _animated_chunk_signature(converted_chunk)
        if signature in seen_animated_signatures:
            return False
        seen_animated_signatures.add(signature)
        chunk_append(new_mesh, source_chunk, converted_chunk, added_chunks)
        return True

    def _coloring_entry_key(entry):
        if isinstance(entry, dict):
            return (str(entry.get("appearance", "")), str(entry.get("componentName", "")))
        return (str(getattr(entry, "appearance", "")), str(getattr(entry, "componentName", "")))

    def _iter_related_w2_entities():
        for depot_path, full_path, related_file in w2_related_files:
            norm_full_path = os.path.normcase(os.path.normpath(full_path))
            if norm_full_path in inherit_visited:
                continue
            try:
                related_entity = create_CEntity(
                    related_file,
                    _inherit_visited=inherit_visited | {norm_full_path},
                )
            except Exception as e:
                log.debug("Failed to build related Witcher 2 entity '%s': %s", depot_path, e)
                continue
            yield depot_path, related_entity

    def _merge_related_inventory_definitions(target_defs, source_defs):
        target_defs = list(target_defs or [])
        seen = {
            tuple(sorted((str(getattr(entry, "category", "")), str(getattr(getattr(entry, "initializer", None), "itemName", "") or str(getattr(entry, "item", "")))) for entry in getattr(inv_def, "entries", []) or []))
            for inv_def in target_defs
        }
        for source_def in source_defs or []:
            signature = tuple(sorted((str(getattr(entry, "category", "")), str(getattr(getattr(entry, "initializer", None), "itemName", "") or str(getattr(entry, "item", "")))) for entry in getattr(source_def, "entries", []) or []))
            if signature in seen:
                continue
            seen.add(signature)
            target_defs.append(copy.deepcopy(source_def))
        return target_defs

    def _merge_related_appearances(source_entity):
        source_apps = list(getattr(source_entity, "appearances", []) or [])
        if not source_apps:
            return
        if not this_Entity.appearances:
            this_Entity.appearances = copy.deepcopy(source_apps)
            return
        target_by_name = {
            str(getattr(app, "name", "")).lower(): app
            for app in this_Entity.appearances
        }
        for source_app in source_apps:
            source_name = str(getattr(source_app, "name", "")).lower()
            target_app = target_by_name.get(source_name)
            if not target_app:
                this_Entity.appearances.append(copy.deepcopy(source_app))
                continue
            if not getattr(target_app, "includedTemplates", None) and getattr(source_app, "includedTemplates", None):
                target_app.includedTemplates = copy.deepcopy(source_app.includedTemplates)
            if not getattr(target_app, "appearanceParams", None) and getattr(source_app, "appearanceParams", None):
                target_app.appearanceParams = copy.deepcopy(source_app.appearanceParams)
            if not getattr(target_app, "inventoryDefinitions", None) and getattr(source_app, "inventoryDefinitions", None):
                target_app.inventoryDefinitions = copy.deepcopy(source_app.inventoryDefinitions)
            if not getattr(target_app, "headName", None) and getattr(source_app, "headName", None):
                target_app.headName = source_app.headName

    def _merge_inherited_coloring_entries():
        if file.HEADER.version <= 115 or not top_level_template_includes:
            return
        existing_keys = {_coloring_entry_key(e) for e in (this_Entity.coloringEntries or [])}
        for include_path in top_level_template_includes:
            depot_path = str(include_path or "").strip()
            if not depot_path or not depot_path.lower().endswith(".w2ent"):
                continue
            try:
                include_full_path = repo_file(depot_path, file.HEADER.version)
                norm_include_path = os.path.normcase(os.path.normpath(include_full_path))
            except Exception as e:
                log.debug(f"Failed to resolve included template path '{depot_path}': {e}")
                continue
            if norm_include_path in inherit_visited:
                continue
            try:
                include_cr2w = read_CR2W(include_full_path)
                include_entity = create_CEntity(include_cr2w, _inherit_visited=inherit_visited | {norm_include_path})
            except Exception as e:
                log.debug(f"Failed to load included template '{depot_path}' for inherited coloringEntries: {e}")
                continue
            for inherited_entry in getattr(include_entity, "coloringEntries", []) or []:
                key = _coloring_entry_key(inherited_entry)
                if key in existing_keys:
                    continue
                this_Entity.coloringEntries.append(inherited_entry)
                existing_keys.add(key)
    
    #ReadTemplate(file, new_mesh, this_Entity)
    if file.HEADER.version <= 115:
        ## Witcher 2 has CExternalProxyComponent that replaces chunks with chunks in the templates include
        #CExternalProxyAttachment + orginal makes for final attachment
        guids = {}

        w2_related_files = _load_w2_related_files_recursive(file, inherit_visited)
        w2_related_entity_paths = [depot_path for depot_path, _full_path, _related_file in w2_related_files]
        w2_related_search_chunks = [
            chunk
            for _depot_path, _full_path, related_file in w2_related_files
            for chunk in related_file.CHUNKS.CHUNKS
        ]

        for template_chunk in CHUNKS:
            if template_chunk.name == "CEntityTemplate":
                w2_body_part_chunk_indices.update(
                    _collect_w2_body_part_chunk_indices(template_chunk, CHUNKS)
                )
                w2_body_part_component_names.update(
                    _collect_w2_body_part_component_names(template_chunk)
                )

        for related_chunk in w2_related_search_chunks:
            if related_chunk.name == "CEntityTemplate":
                w2_body_part_component_names.update(
                    _collect_w2_body_part_component_names(related_chunk)
                )

        for chunk in CHUNKS:
            if chunk.name == "CExternalProxyComponent":
                guids[chunk.GetVariableByName("guid").GUID.GuidString] = chunk
        for _depot_path, _full_path, related_file in w2_related_files:
            for chunk in related_file.CHUNKS.CHUNKS:
                if chunk.GetVariableByName("guid"):
                    if chunk.GetVariableByName("guid").GUID.GuidString in guids:
                        old_chunk = guids[chunk.GetVariableByName("guid").GUID.GuidString]
                        chunk.ChunkIndex = old_chunk.ChunkIndex
                        guids[chunk.GetVariableByName("guid").GUID.GuidString] = chunk
                        CHUNKS[chunk.ChunkIndex] = chunk
    
        #CExternalProxyAttachments = {}
        for chunk in CHUNKS:
            if chunk.name == "CExternalProxyAttachment":
                attachment = CHUNKS[chunk.GetVariableByName("originalAttachment").Value-1]
                attachment.PROPS.extend(chunk.PROPS)
                #CExternalProxyAttachments[chunk.ChunkIndex] = (chunk, attachment)

    def _resolve_initializer_chunk(init_prop):
        if not init_prop:
            return None

        ptr = None
        if hasattr(init_prop, "Value") and isinstance(init_prop.Value, int):
            ptr = init_prop.Value
        elif hasattr(init_prop, "value"):
            init_value = getattr(init_prop, "value", None)
            if isinstance(init_value, int):
                ptr = init_value
            elif isinstance(init_value, list):
                for candidate in init_value:
                    if isinstance(candidate, int) and candidate > 0:
                        ptr = candidate
                        break

        if (not isinstance(ptr, int) or ptr <= 0) and hasattr(init_prop, "Handles") and init_prop.Handles:
            first_handle = init_prop.Handles[0]
            handle_ptr = getattr(first_handle, "val", None)
            if not isinstance(handle_ptr, int) or handle_ptr <= 0:
                ref_idx = getattr(first_handle, "Reference", None)
                if isinstance(ref_idx, int) and ref_idx >= 0:
                    handle_ptr = ref_idx + 1
            if isinstance(handle_ptr, int) and handle_ptr > 0:
                ptr = handle_ptr

        if not isinstance(ptr, int) or ptr <= 0 or ptr > len(CHUNKS):
            return None
        return CHUNKS[ptr - 1]

    def _resolve_inventory_initializer(inv_entry):
        init_chunk = _resolve_initializer_chunk(getattr(inv_entry, "initializer", None))
        if not init_chunk:
            return None
        if init_chunk.Type == "CInventoryInitializerUniform":
            return w3_types.CInventoryInitializerUniform(init_chunk)
        if init_chunk.Type == "CInventoryInitializerRandom":
            return w3_types.CInventoryInitializerRandom(init_chunk)
        # Last parsed-data path: instantiate a matching initializer type when available.
        try:
            return w3_types.str_to_class(init_chunk.Type)(init_chunk)
        except Exception:
            return None

    def _resolve_equipment_initializer(equip_entry):
        init_chunk = _resolve_initializer_chunk(getattr(equip_entry, "initializer", None))
        if not init_chunk:
            return None
        if init_chunk.Type == "CEquipmentInitializerUniform":
            return w3_types.CEquipmentInitializerUniform(init_chunk)
        if init_chunk.Type == "CEquipmentInitializerRandom":
            return w3_types.CEquipmentInitializerRandom(init_chunk)
        try:
            return w3_types.str_to_class(init_chunk.Type)(init_chunk)
        except Exception:
            return None

    def _parse_inventory_definition(def_chunk):
        final_inv_entries = []
        inv_def = w3_types.CInventoryDefinition(def_chunk)
        if inv_def.entries:
            entry_ptrs = []
            if hasattr(inv_def.entries, "value") and inv_def.entries.value:
                entry_ptrs = inv_def.entries.value
            elif hasattr(inv_def.entries, "Handles") and inv_def.entries.Handles:
                entry_ptrs = [handle.val for handle in inv_def.entries.Handles]
            for inv_ptr in entry_ptrs:
                if not isinstance(inv_ptr, int) or inv_ptr <= 0 or inv_ptr > len(CHUNKS):
                    continue
                inv_entry = w3_types.CInventoryDefinitionEntry(CHUNKS[inv_ptr-1])
                resolved_init = _resolve_inventory_initializer(inv_entry)
                if resolved_init:
                    inv_entry.initializer = resolved_init
                final_inv_entries.append(inv_entry)
        setattr(inv_def, 'entries', final_inv_entries)
        return inv_def

    for chunk in CHUNKS:
        if chunk.Type == "CEntityTemplate":
            includes = chunk.GetVariableByName("includes")
            if includes and hasattr(includes, "Handles"):
                for include in includes.Handles:
                    depot_path = _resolve_handle_repo_path(chunk, include, ".w2ent")
                    if not depot_path:
                        continue
                    depot_key = str(depot_path).lower()
                    if depot_key in top_level_template_include_set:
                        continue
                    top_level_template_include_set.add(depot_key)
                    top_level_template_includes.append(depot_path)

            template_params = chunk.GetVariableByName("templateParams")
            log.debug(f"CEntityTemplate found, templateParams={template_params}")
            if template_params:
                log.debug(f"  templateParams type={type(template_params)}, has value={hasattr(template_params, 'value')}, has More={hasattr(template_params, 'More')}")
                # Try both .value and .More accessors for array elements
                params_array = None
                if hasattr(template_params, "value") and template_params.value:
                    params_array = template_params.value
                    log.debug(f"  Using templateParams.value: {params_array}")
                elif hasattr(template_params, "More") and template_params.More:
                    # .More typically contains objects, need to get their pointer values
                    params_array = []
                    for param in template_params.More:
                        if hasattr(param, "Value"):
                            params_array.append(param.Value)
                        elif hasattr(param, "ChunkIndex"):
                            params_array.append(param.ChunkIndex + 1)  # ChunkIndex is 0-based, ptr is 1-based
                    log.debug(f"  Using templateParams.More: {params_array}")

                if params_array:
                    for ptr in params_array:
                        log.debug(f"    ptr={ptr}, CHUNKS count={len(CHUNKS)}")
                        if isinstance(ptr, int) and ptr > 0 and ptr <= len(CHUNKS):
                            def_chunk = CHUNKS[ptr - 1]
                            log.debug(f"    def_chunk.Type={def_chunk.Type}")
                            if def_chunk.Type == "CInventoryDefinition":
                                inv_def = _parse_inventory_definition(def_chunk)
                                if not hasattr(this_Entity, "inventoryDefinitions"):
                                    this_Entity.inventoryDefinitions = []
                                this_Entity.inventoryDefinitions.append(inv_def)
                                log.info(f"Added inventory definition with {len(inv_def.entries) if inv_def.entries else 0} entries")

    # Secondary parsed-data pass for cooked files that expose inventory definitions directly.
    if not hasattr(this_Entity, "inventoryDefinitions") or not this_Entity.inventoryDefinitions:
        log.debug("No inventoryDefinitions found via templateParams, checking direct chunks")
        for chunk in CHUNKS:
            if chunk.Type == "CInventoryDefinition":
                log.debug(f"Found direct CInventoryDefinition chunk: {chunk}")
                inv_def = _parse_inventory_definition(chunk)
                if not hasattr(this_Entity, "inventoryDefinitions"):
                    this_Entity.inventoryDefinitions = []
                this_Entity.inventoryDefinitions.append(inv_def)
                log.info(f"Added inventory definition (direct) with {len(inv_def.entries) if inv_def.entries else 0} entries")

    for chunk in CHUNKS:
        if chunk.Type in {"CEntityTemplate", "CEntityExternalAppearance"}:
            slots = chunk.GetVariableByName("slots")
            if slots:
                for slot in _iter_struct_items(slots):
                    currentSlot = w3_types.EntitySlot(False, slot)
                    currentSlot.transform = currentSlot.transform.EngineTransform if currentSlot.transform else None
                    this_Entity.slots.append(currentSlot)

            if chunk.Type != "CEntityExternalAppearance" and not chunk.GetVariableByName("appearances"):
                continue

            if chunk.Type == "CEntityExternalAppearance":
                appearances = [chunk.GetVariableByName("appearance")]
            else:
                appearances = chunk.GetVariableByName("appearances").More
            for appearance in appearances:
                currentApp = w3_types.CEntityAppearance(False, appearance)
                if currentApp.includedTemplates:
                    final_includedTemplates = []
                    includedTemplates = currentApp.includedTemplates.ToArray()
                    for entryTemplate in includedTemplates:
                        entry = entryTemplate.DepotPath
                        (templateMesh, entity) = LoadCEntityTemplateFile(entry)
                        final_includedTemplates.append(templateMesh)
                    setattr(currentApp, 'includedTemplates', final_includedTemplates) # Replace pointers with chunks
                elif appearance.GetVariableByName("parts"): #!WITCHER 2
                    pending_w2_appearances.append((chunk, appearance, currentApp))
                else:
                    #some "invisible" appearances have no entities attached
                    log.warning("Entity has no includedTemplates")
                    #GetFace(@"characters\models\geralt\head\model\h_01_mg__geralt.w3fac")
                if currentApp.appearanceParams:
                    final_CEquipmentDefinitions = []
                    for ptr in currentApp.appearanceParams.value:
                        def_chunk = CHUNKS[ptr-1]
                        if def_chunk.Type == 'CEquipmentDefinition':
                            final_entries = []
                            CEquipmentDefinition = w3_types.CEquipmentDefinition(def_chunk)
                            if CEquipmentDefinition.entries:
                                entry_ptrs = []
                                if hasattr(CEquipmentDefinition.entries, "value") and CEquipmentDefinition.entries.value:
                                    entry_ptrs = CEquipmentDefinition.entries.value
                                elif hasattr(CEquipmentDefinition.entries, "Handles") and CEquipmentDefinition.entries.Handles:
                                    entry_ptrs = [handle.val for handle in CEquipmentDefinition.entries.Handles]
                                for ptr in entry_ptrs:
                                    if not isinstance(ptr, int) or ptr <= 0 or ptr > len(CHUNKS):
                                        continue
                                    entry = w3_types.CEquipmentDefinitionEntry(CHUNKS[ptr-1])
                                    resolved_init = _resolve_equipment_initializer(entry)
                                    if resolved_init:
                                        entry.initializer = resolved_init
                                        if not getattr(entry, "defaultItemName", None):
                                            init_item_name = getattr(resolved_init, "itemName", None) or getattr(resolved_init, "item", None)
                                            if init_item_name:
                                                entry.defaultItemName = init_item_name
                                    final_entries.append(entry)
                            setattr(CEquipmentDefinition, 'entries', final_entries) # Replace pointers with chunks
                            final_CEquipmentDefinitions.append(CEquipmentDefinition)
                        elif def_chunk.Type == 'CInventoryDefinition':
                            # Parse inventory definition for auto-mount items
                            inv_def = _parse_inventory_definition(def_chunk)
                            # Store on appearance for later processing
                            if not hasattr(currentApp, 'inventoryDefinitions'):
                                currentApp.inventoryDefinitions = []
                            currentApp.inventoryDefinitions.append(inv_def)
                    setattr(currentApp, 'appearanceParams', final_CEquipmentDefinitions) # Replace pointers with chunks
                this_Entity.appearances.append(currentApp)
                #print(appearance.elementName)
                
            coloringEntries = chunk.GetVariableByName("coloringEntries")
            if coloringEntries:
                for coloringEntry in coloringEntries.More:
                    if coloringEntries.Count == 1:
                        coloringEntry = coloringEntries
                    colorShift1 = coloringEntry.GetVariableByName('colorShift1')
                    if colorShift1:
                        colorShift1 = w3_types.CColorShift(colorShift1.GetVariableByName('hue').Value if colorShift1.GetVariableByName('hue') else 0,
                                                           colorShift1.GetVariableByName('saturation').Value if colorShift1.GetVariableByName('saturation') else 0,
                                                           colorShift1.GetVariableByName('luminance').Value if colorShift1.GetVariableByName('luminance') else 0)
                    colorShift2 = coloringEntry.GetVariableByName('colorShift2')
                    if colorShift2:
                        colorShift2 =  w3_types.CColorShift(colorShift2.GetVariableByName('hue').Value if colorShift2.GetVariableByName('hue') else 0,
                                                           colorShift2.GetVariableByName('saturation').Value if colorShift2.GetVariableByName('saturation') else 0,
                                                           colorShift2.GetVariableByName('luminance').Value if colorShift2.GetVariableByName('luminance') else 0)
                    this_Entity.coloringEntries.append(
                        w3_types.SEntityTemplateColoringEntry(
                            coloringEntry.GetVariableByName('appearance').ToString(),
                            coloringEntry.GetVariableByName('componentName').ToString(),
                            colorShift1,
                            colorShift2))
                        # { 'name': "MimicSets",
                        #   'animationSets':list(map(lambda x: x.DepotPath, chunk.GetVariableByName("animationSets").ToArray()))
                        # })

        elif chunk.Type in Entity_Type_List: #entity is
            entity_chunk = chunk  # save before inner loop reassigns chunk variable
            entity_animated_component_chunk_index = None  # track for synthetic skinning attachment
            if hasattr(chunk, 'Components'):
            #for staticChunkPtr in chunk.GetVariableByName("components").ToArray():
                if not chunk.Components and mesh_import_paths:
                    log_fn = log.debug if file.HEADER.version <= 115 else log.warning
                    log_fn(
                        f"{chunk.Type} has empty Components list while mesh imports exist "
                        f"({len(mesh_import_paths)}), using top-level mesh fallback: {file.fileName}"
                    )
                for chunk_idx in chunk.Components:
                    chunk = CHUNKS[chunk_idx-1] #staticChunkPtr.Reference
                    chunk_name = _prop_to_string(_find_prop_by_name(chunk, "name"))
                    if chunk.ChunkIndex in w2_body_part_chunk_indices or str(chunk_name or "").strip().lower() in w2_body_part_component_names:
                        continue
                    if (chunk.Type == "CStaticMeshComponent"):
                        static_mesh_component = CStaticMeshComponent(chunk).convert_for_io()
                        static_mesh_component.mesh = _resolve_mesh_path(chunk, static_mesh_component.mesh)
                        if not static_mesh_component.mesh:
                            static_mesh_component.mesh = _next_mesh_import_path()
                        if static_mesh_component.mesh:
                            _append_unique_chunk(chunk, static_mesh_component, added_chunks)
                        else:
                            log.warning(
                                f"Skipping CStaticMeshComponent with invalid mesh ref: {chunk.ChunkIndex}; "
                                f"props={_chunk_props_summary(chunk)}"
                            )
                    elif (chunk.Type == "CMeshComponent"):
                        mesh_component = CMeshComponent(chunk).convert_for_io()
                        mesh_component.mesh = _resolve_mesh_path(chunk, mesh_component.mesh)
                        if not mesh_component.mesh:
                            mesh_component.mesh = _next_mesh_import_path()
                        if mesh_component.mesh:
                            _append_unique_chunk(chunk, mesh_component, added_chunks)
                        else:
                            log.warning(
                                f"Skipping CMeshComponent with invalid mesh ref: {chunk.ChunkIndex}; "
                                f"props={_chunk_props_summary(chunk)}"
                            )
                    elif (chunk.Type == "CRigidMeshComponent" or chunk.Type == "CRagdollMeshComponent"):
                        mesh_component = CMeshComponent(chunk).convert_for_io()
                        mesh_component.mesh = _resolve_mesh_path(chunk, mesh_component.mesh)
                        if not mesh_component.mesh:
                            mesh_component.mesh = _next_mesh_import_path()
                        if mesh_component.mesh:
                            _append_unique_chunk(chunk, mesh_component, added_chunks)
                        else:
                            log.warning(
                                f"Skipping {chunk.Type} with invalid mesh ref: {chunk.ChunkIndex}; "
                                f"props={_chunk_props_summary(chunk)}"
                            )
                    elif (chunk.Type == "CFurComponent"):
                        fur_component = CMeshComponent(chunk).convert_for_io()
                        fur_component.mesh = _resolve_mesh_path(chunk, fur_component.mesh)
                        if not fur_component.mesh:
                            fur_component.mesh = _next_mesh_import_path()
                        if fur_component.mesh:
                            _append_unique_chunk(chunk, fur_component, added_chunks)
                        else:
                            log.warning(
                                f"Skipping CFurComponent with invalid mesh ref: {chunk.ChunkIndex}; "
                                f"props={_chunk_props_summary(chunk)}"
                            )
                    elif chunk.Type == "CPointLightComponent":
                        _append_unique_light_chunk(chunk, CPointLightComponent(chunk).convert_for_io(), added_chunks)
                    elif chunk.Type == "CSpotLightComponent":
                        _append_unique_light_chunk(chunk, CSpotLightComponent(chunk).convert_for_io(), added_chunks)
                    elif (chunk.Type == "CAnimatedComponent"):
                        animated_component = CAnimatedComponent(chunk).convert_for_io()
                        name = chunk.GetVariableByName("name").ToString()
                        skeleton = _resolve_repo_path(chunk, "skeleton", ".w2rig")
                        animation_sets = _resolve_repo_paths_from_array(chunk, "animationSets", ".w2anims")
                        if not skeleton:
                            # Component may be an override chunk that stores only
                            # non-skeleton properties (e.g. transform). Fall back to
                            # the first CSkeleton referenced in the file's import table.
                            rig_paths = _collect_rig_import_paths(file)
                            if rig_paths:
                                skeleton = rig_paths[0]
                                log.debug(
                                    f"CAnimatedComponent #{chunk.ChunkIndex} has no skeleton "
                                    f"property; using import-table fallback: {skeleton}"
                                )
                        entity_animated_component_chunk_index = chunk.ChunkIndex
                        animated_component.name = name or animated_component.name
                        animated_component.skeleton = skeleton
                        animated_component.animationSets = animation_sets
                        _append_unique_animated_chunk(chunk, animated_component, added_chunks)
                    elif (chunk.Type == "CCameraComponent"):
                        name = chunk.GetVariableByName("name").ToString()
                        chunk_append(new_mesh, chunk, CCameraComponent(name), added_chunks)
            # Cooked item entities (Crossbow, CItemEntity, CWitcherSword, etc.) store
            # their mesh inside a SharedDataBuffer rather than as a direct Component.
            # Check the saved entity chunk for a streamingDataBuffer after processing Components.
            if entity_chunk.Type in _STREAMED_ITEM_CHUNK_TYPES:
                sdb = entity_chunk.GetVariableByName('streamingDataBuffer')
                if sdb and hasattr(sdb, 'Bufferdata') and hasattr(sdb.Bufferdata, 'Bytes'):
                    try:
                        buf_stream = bStream(data=bytearray(sdb.Bufferdata.Bytes))
                        buf_stream.name = 'streamingDataBuffer'
                        buf_cr2w = getCR2W(buf_stream)
                        for buf_chunk in buf_cr2w.CHUNKS.CHUNKS:
                            if buf_chunk.Type == 'CMeshComponent':
                                mc = CMeshComponent(buf_chunk).convert_for_io()
                                mc.mesh = _resolve_mesh_path(buf_chunk, mc.mesh)
                                if not mc.mesh:
                                    mc.mesh = _next_mesh_import_path()
                                if mc.mesh and mc.mesh not in seen_streamed_mesh_paths:
                                    chunk_append(new_mesh, entity_chunk, mc, added_chunks)
                                    new_mesh.chunks[-1].type = buf_chunk.Type
                                    seen_streamed_mesh_paths.add(mc.mesh)
                                    log.debug(
                                        'Extracted CMeshComponent from streamingDataBuffer of %s #%s: %s',
                                        entity_chunk.Type, entity_chunk.ChunkIndex, mc.mesh,
                                    )
                                    # Synthesize a CMeshSkinningAttachment to bind the rig
                                    # (CAnimatedComponent) to this mesh, mirroring what lvl2/3
                                    # have as explicit CR2W chunks.
                                    if entity_animated_component_chunk_index is not None:
                                        skinning = CMeshSkinningAttachment(
                                            entity_animated_component_chunk_index,
                                            entity_chunk.ChunkIndex,
                                        )
                                        skinning.type = 'CMeshSkinningAttachment'
                                        skinning.chunkIndex = -1  # synthetic, no real CR2W chunk
                                        new_mesh.chunks.append(skinning)
                    except Exception as e:
                        log.warning(
                            'Failed to parse streamingDataBuffer for %s #%s: %s',
                            entity_chunk.Type, entity_chunk.ChunkIndex, e,
                        )
                else:
                    log.debug(
                        '%s #%s: streamingDataBuffer not in PROPS or has no Bytes '
                        '(sdb=%s, PROPS=%s); mesh sourced from flatCompiledData if available.',
                        entity_chunk.Type, entity_chunk.ChunkIndex,
                        sdb, _chunk_props_summary(entity_chunk),
                    )
        elif (chunk.Type == "CHardAttachment"):
            #if (chunk.GetVariableByName("parentSlot")): 
            chunk_append(new_mesh, chunk, CHardAttachment(chunk).convert_for_io())
        elif (chunk.Type == "CMeshSkinningAttachment"):
            parent_var = chunk.GetVariableByName("parent")
            child_var = chunk.GetVariableByName("child")
            if parent_var and child_var:
                chunk_append(new_mesh, chunk, CMeshSkinningAttachment(parent_var.Value-1, child_var.Value-1))
            else:
                log.warning(f'CMeshSkinningAttachment missing parent or child at chunk {chunk.ChunkIndex}')
        elif (chunk.Type == "CSkeletonBoneSlot"):
            boneIndex = chunk.GetVariableByName("boneIndex").Value #val?
            chunk_append(new_mesh, chunk, CSkeletonBoneSlot(boneIndex))
        elif(chunk.name == "CMovingPhysicalAgentComponent" and chunk.GetVariableByName("skeleton")):
            name = chunk.GetVariableByName("name").ToString()
            skeleton = _resolve_repo_path(chunk, "skeleton", ".w2rig")
            moving_component = w3_types.CMovingPhysicalAgentComponent(skeleton, name)
            moving_component.animationSets = _resolve_repo_paths_from_array(chunk, "animationSets", ".w2anims")
            chunk_append(new_mesh, chunk, moving_component)
            hasCMovingPhysicalAgentComponent = True;
            this_Entity.MovingPhysicalAgentComponent= new_mesh.chunks[-1]
        elif(chunk.name == "CAnimAnimsetsParam"):
            if chunk.GetVariableByName("animationSets"):
                this_Entity.CAnimAnimsetsParam.append({
                    'name': chunk.GetVariableByName("name").ToString(),
                    'componentName': chunk.GetVariableByName("componentName").ToString() if chunk.GetVariableByName("componentName") else "",
                    'animationSets': _resolve_repo_paths_from_array(chunk, "animationSets", ".w2anims"),
                })
        elif(chunk.name == "CAnimMimicParam"):
            if chunk.GetVariableByName("animationSets"):
                this_Entity.CAnimMimicParam.append({ 'name': "MimicSets",
                                                'animationSets':_resolve_repo_paths_from_array(chunk, "animationSets", ".w2anims")
                                            })
        
        ############
        #ITEMS FROM MESH
        #
        #######
        # Only add top-level mesh chunks if they weren't already added via CEntity.Components
        elif (chunk.Type == "CMeshComponent") and chunk.ChunkIndex not in added_chunks and chunk.ChunkIndex not in w2_body_part_chunk_indices and str(_prop_to_string(_find_prop_by_name(chunk, "name")) or "").strip().lower() not in w2_body_part_component_names:
            mc = CMeshComponent(chunk).convert_for_io()
            mc.mesh = _resolve_mesh_path(chunk, mc.mesh)
            if not mc.mesh:
                mc.mesh = _next_mesh_import_path()
            if mc.mesh:
                _append_unique_chunk(chunk, mc, added_chunks)
            else:
                log.warning(
                    f"Skipping top-level CMeshComponent with invalid mesh ref at chunk {chunk.ChunkIndex}; "
                    f"props={_chunk_props_summary(chunk)}"
                )
        elif (chunk.Type == "CRigidMeshComponent" or chunk.Type == "CRagdollMeshComponent") and chunk.ChunkIndex not in added_chunks and chunk.ChunkIndex not in w2_body_part_chunk_indices and str(_prop_to_string(_find_prop_by_name(chunk, "name")) or "").strip().lower() not in w2_body_part_component_names:
            mc = CMeshComponent(chunk).convert_for_io()
            mc.mesh = _resolve_mesh_path(chunk, mc.mesh)
            if not mc.mesh:
                mc.mesh = _next_mesh_import_path()
            if mc.mesh:
                _append_unique_chunk(chunk, mc, added_chunks)
            else:
                log.warning(
                    f"Skipping top-level {chunk.Type} with invalid mesh ref at chunk {chunk.ChunkIndex}; "
                    f"props={_chunk_props_summary(chunk)}"
                )
        elif (chunk.Type == "CClothComponent") and chunk.ChunkIndex not in added_chunks and chunk.ChunkIndex not in w2_body_part_chunk_indices and str(_prop_to_string(_find_prop_by_name(chunk, "name")) or "").strip().lower() not in w2_body_part_component_names:
            if chunk.GetVariableByName("resource"): #! sometimes there are no resource in files??
                cloth = chunk.GetVariableByName("resource").ToString()
                chunk_append(new_mesh, chunk, CClothComponent(cloth), added_chunks)
        elif (chunk.Type == "CFurComponent") and chunk.ChunkIndex not in added_chunks and chunk.ChunkIndex not in w2_body_part_chunk_indices and str(_prop_to_string(_find_prop_by_name(chunk, "name")) or "").strip().lower() not in w2_body_part_component_names:
            if (chunk.GetVariableByName("mesh")):
                fur_component = CMeshComponent(chunk).convert_for_io()
                fur_component.mesh = _resolve_mesh_path(chunk, fur_component.mesh)
                if not fur_component.mesh:
                    fur_component.mesh = _next_mesh_import_path()
                if fur_component.mesh:
                    _append_unique_chunk(chunk, fur_component, added_chunks)
                else:
                    log.warning(
                        f"Skipping top-level CFurComponent with invalid mesh ref: {chunk.ChunkIndex}; "
                        f"props={_chunk_props_summary(chunk)}"
                    )
        elif (chunk.Type == "CPointLightComponent") and chunk.ChunkIndex not in added_chunks and chunk.ChunkIndex not in w2_body_part_chunk_indices and str(_prop_to_string(_find_prop_by_name(chunk, "name")) or "").strip().lower() not in w2_body_part_component_names:
            _append_unique_light_chunk(chunk, CPointLightComponent(chunk).convert_for_io(), added_chunks)
        elif (chunk.Type == "CSpotLightComponent") and chunk.ChunkIndex not in added_chunks and chunk.ChunkIndex not in w2_body_part_chunk_indices and str(_prop_to_string(_find_prop_by_name(chunk, "name")) or "").strip().lower() not in w2_body_part_component_names:
            _append_unique_light_chunk(chunk, CSpotLightComponent(chunk).convert_for_io(), added_chunks)
        elif (chunk.Type == "CAnimatedComponent") and chunk.ChunkIndex not in added_chunks and chunk.ChunkIndex not in w2_body_part_chunk_indices and str(_prop_to_string(_find_prop_by_name(chunk, "name")) or "").strip().lower() not in w2_body_part_component_names:
            animated_component = CAnimatedComponent(chunk).convert_for_io()
            name = chunk.GetVariableByName("name").ToString()
            skeleton = _resolve_repo_path(chunk, "skeleton", ".w2rig")
            animation_sets = _resolve_repo_paths_from_array(chunk, "animationSets", ".w2anims")
            if not skeleton:
                rig_paths = _collect_rig_import_paths(file)
                if rig_paths:
                    skeleton = rig_paths[0]
                    log.debug(
                        f"CAnimatedComponent #{chunk.ChunkIndex} has no skeleton "
                        f"property; using import-table fallback: {skeleton}"
                    )
            animated_component.name = name or animated_component.name
            animated_component.skeleton = skeleton
            animated_component.animationSets = animation_sets
            _append_unique_animated_chunk(chunk, animated_component, added_chunks)
        elif (chunk.Type == "CMorphedMeshComponent"):
            morphTarget = _resolve_repo_path(chunk, "morphTarget", ".w2mesh")
            morphSource = _resolve_repo_path(chunk, "morphSource", ".w2mesh")
            morphComponentId = chunk.GetVariableByName("morphComponentId").ToString()
            chunk_append(new_mesh, chunk, CMorphedMeshComponent(morphTarget, morphSource, morphComponentId))
        elif (chunk.Type == "CMimicComponent"):
            name = chunk.GetVariableByName("name").ToString()
            mimicFace = (
                _resolve_repo_path(chunk, "mimicFace", ".w3fac")
                or _resolve_repo_path(chunk, "mimicFace", ".w2fac")
            )
            chunk_append(new_mesh, chunk, CMimicComponent(name, mimicFace))
            #TODO GetFACE needed?
            #new_mesh.animation_face_object = GetFace(mimicFace)
        # elif (chunk.Type == "CMeshSkinningAttachment"):
        #     parent = chunk.GetVariableByName("parent").Value-1
        #     child = chunk.GetVariableByName("child").Value-1
        #     chunk_append(new_mesh, chunk, CMeshSkinningAttachment(parent, child))
        elif (chunk.Type == "CAnimatedAttachment"):
            parent_var = chunk.GetVariableByName("parent")
            child_var = chunk.GetVariableByName("child")
            if parent_var and child_var:
                chunk_append(new_mesh, chunk, CAnimatedAttachment(parent_var.Value-1, child_var.Value-1))
            else:
                log.warning(f'CAnimatedAttachment missing parent or child at chunk {chunk.ChunkIndex}')
        elif (chunk.Type == "CAnimDangleBufferComponent"):
            name = chunk.GetVariableByName("name").ToString()
            skeleton = _resolve_repo_path(chunk, "skeleton", ".w2rig")
            chunk_append(new_mesh, chunk, CAnimDangleBufferComponent(name, skeleton))
        elif (chunk.Type == "CAnimDangleComponent"):
            name = chunk.GetVariableByName("name").ToString()
            constraint_var = chunk.GetVariableByName("constraint")
            constraint = constraint_var.Value - 1 if constraint_var else None
            chunk_append(new_mesh, chunk, CAnimDangleComponent(name, constraint))
        elif (chunk.Type == "CAnimDangleConstraint_Dyng"):
            dyng = _resolve_repo_path(chunk, "dyng", ".w3dyng") if chunk.GetVariableByName("dyng") else None
            if not dyng and chunk.GetVariableByName("dyng"):
                dyng = _resolve_repo_path(chunk, "dyng", ".dyng")
            skeleton = _resolve_repo_path(chunk, "skeleton", ".w2rig") if chunk.GetVariableByName("skeleton") else None
            chunk_append(new_mesh, chunk, CAnimDangleConstraint_Dyng(skeleton, dyng))
        elif (chunk.Type in CAnimDangleConstraint_types):
            skeleton = _resolve_repo_path(chunk, "skeleton", ".w2rig")
            chunk_append(new_mesh, chunk, CAnimDangleConstraint_types[chunk.Type](skeleton))
        # elif (chunk.Type == "CHardAttachment"): #TODO NormalBlend Stuff
        #     if (chunk.GetVariableByName("parentSlot")):
        #         chunk_append(new_mesh, chunk, CHardAttachment(chunk).convert_for_io())
        ############
        #ITEMS FROM MESH END
        #
        #######


    # Process flatCompiledData sub-CR2W chunks (cooked entity files).
    # Cooked .w2ent files store mesh components in an embedded sub-CR2W
    # (flatCompiledData on CEntityTemplate), parsed by CR2W_types.
    for chunk in CHUNKS:
        if chunk.Type == "CEntityTemplate" and hasattr(chunk, "flatCompiledData") and chunk.flatCompiledData is not None:
            sub_chunks = chunk.flatCompiledData.CHUNKS.CHUNKS
            for sub_chunk in sub_chunks:
                try:
                    if sub_chunk.Type == "CMeshComponent":
                        mesh_component = CMeshComponent(sub_chunk).convert_for_io()
                        mesh_component.mesh = _resolve_mesh_path(sub_chunk, mesh_component.mesh)
                        if not mesh_component.mesh:
                            mesh_component.mesh = _next_mesh_import_path()
                        if mesh_component.mesh and mesh_component.mesh not in seen_streamed_mesh_paths:
                            _append_unique_chunk(sub_chunk, mesh_component)
                        elif not mesh_component.mesh:
                            log.warning(
                                f"Skipping flatCompiledData CMeshComponent with invalid mesh ref: {sub_chunk.ChunkIndex}; "
                                f"props={_chunk_props_summary(sub_chunk)}"
                            )
                    elif sub_chunk.Type == "CRigidMeshComponent" or sub_chunk.Type == "CRagdollMeshComponent":
                        mesh_component = CMeshComponent(sub_chunk).convert_for_io()
                        mesh_component.mesh = _resolve_mesh_path(sub_chunk, mesh_component.mesh)
                        if not mesh_component.mesh:
                            mesh_component.mesh = _next_mesh_import_path()
                        if mesh_component.mesh:
                            _append_unique_chunk(sub_chunk, mesh_component)
                        else:
                            log.warning(
                                f"Skipping flatCompiledData {sub_chunk.Type} with invalid mesh ref: {sub_chunk.ChunkIndex}; "
                                f"props={_chunk_props_summary(sub_chunk)}"
                            )
                    elif sub_chunk.Type == "CFurComponent" and sub_chunk.GetVariableByName("mesh"):
                        fur_component = CMeshComponent(sub_chunk).convert_for_io()
                        fur_component.mesh = _resolve_mesh_path(sub_chunk, fur_component.mesh)
                        if not fur_component.mesh:
                            fur_component.mesh = _next_mesh_import_path()
                        if fur_component.mesh:
                            _append_unique_chunk(sub_chunk, fur_component)
                        else:
                            log.warning(
                                f"Skipping flatCompiledData CFurComponent with invalid mesh ref: {sub_chunk.ChunkIndex}; "
                                f"props={_chunk_props_summary(sub_chunk)}"
                            )
                    elif sub_chunk.Type == "CClothComponent" and sub_chunk.GetVariableByName("resource"):
                        cloth = sub_chunk.GetVariableByName("resource").ToString()
                        chunk_append(new_mesh, sub_chunk, CClothComponent(cloth))
                    elif sub_chunk.Type == "CAnimDangleBufferComponent" and sub_chunk.GetVariableByName("skeleton"):
                        name = sub_chunk.GetVariableByName("name").ToString()
                        skeleton = _resolve_repo_path(sub_chunk, "skeleton", ".w2rig")
                        chunk_append(new_mesh, sub_chunk, CAnimDangleBufferComponent(name, skeleton))
                    elif sub_chunk.Type == "CAnimatedComponent":
                        animated_component = CAnimatedComponent(sub_chunk).convert_for_io()
                        name = sub_chunk.GetVariableByName("name").ToString()
                        skeleton = _resolve_repo_path(sub_chunk, "skeleton", ".w2rig")
                        animation_sets = _resolve_repo_paths_from_array(sub_chunk, "animationSets", ".w2anims")
                        if not skeleton:
                            rig_paths = _collect_rig_import_paths(file)
                            if rig_paths:
                                skeleton = rig_paths[0]
                                log.debug(
                                    f"CAnimatedComponent #{sub_chunk.ChunkIndex} has no skeleton "
                                    f"property; using import-table fallback: {skeleton}"
                                )
                        animated_component.name = name or animated_component.name
                        animated_component.skeleton = skeleton
                        animated_component.animationSets = animation_sets
                        _append_unique_animated_chunk(sub_chunk, animated_component)
                    elif sub_chunk.Type == "CPointLightComponent":
                        _append_unique_light_chunk(sub_chunk, CPointLightComponent(sub_chunk).convert_for_io())
                    elif sub_chunk.Type == "CSpotLightComponent":
                        _append_unique_light_chunk(sub_chunk, CSpotLightComponent(sub_chunk).convert_for_io())
                    elif sub_chunk.Type in _STREAMED_ITEM_CHUNK_TYPES:
                        # Cooked item entities (Crossbow, CWitcherSword, etc.) store their
                        # mesh component inside a SharedDataBuffer (streamingDataBuffer) rather
                        # than as a direct CMeshComponent sub-chunk.  Parse the buffer and pull
                        # out any CMeshComponent chunks found inside.
                        sdb_var = sub_chunk.GetVariableByName("streamingDataBuffer")
                        if not sdb_var:
                            log.debug(
                                "flatCompiledData %s #%s: GetVariableByName('streamingDataBuffer') "
                                "returned None (PROPS=%s); mesh sourced from main CR2W entity chunk.",
                                sub_chunk.Type, sub_chunk.ChunkIndex,
                                _chunk_props_summary(sub_chunk),
                            )
                        if sdb_var and hasattr(sdb_var, "Bufferdata") and hasattr(sdb_var.Bufferdata, "Bytes"):
                            try:
                                buf_stream = bStream(data=bytearray(sdb_var.Bufferdata.Bytes))
                                buf_stream.name = "streamingDataBuffer"
                                buf_cr2w = getCR2W(buf_stream)
                                for buf_chunk in buf_cr2w.CHUNKS.CHUNKS:
                                    if buf_chunk.Type == "CMeshComponent":
                                        mesh_component = CMeshComponent(buf_chunk).convert_for_io()
                                        mesh_component.mesh = _resolve_mesh_path(buf_chunk, mesh_component.mesh)
                                        if not mesh_component.mesh:
                                            mesh_component.mesh = _next_mesh_import_path()
                                        if mesh_component.mesh and mesh_component.mesh not in seen_streamed_mesh_paths:
                                            chunk_append(new_mesh, buf_chunk, mesh_component)
                                            seen_streamed_mesh_paths.add(mesh_component.mesh)
                                            log.debug(
                                                f"Extracted CMeshComponent from streamingDataBuffer "
                                                f"of {sub_chunk.Type} #{sub_chunk.ChunkIndex}: {mesh_component.mesh}"
                                            )
                                        elif not mesh_component.mesh:
                                            log.warning(
                                                f"Skipping streamingDataBuffer CMeshComponent with "
                                                f"invalid mesh ref inside {sub_chunk.Type} #{sub_chunk.ChunkIndex}"
                                            )
                            except Exception as e:
                                log.warning(
                                    f"Failed to parse streamingDataBuffer for {sub_chunk.Type} "
                                    f"#{sub_chunk.ChunkIndex}: {e}"
                                )
                except Exception as e:
                    log.warning(f"Failed to process flatCompiledData chunk {sub_chunk.Type}: {e}")

    if pending_w2_appearances:
        search_chunks = CHUNKS + [chunk for chunk in w2_related_search_chunks if chunk not in CHUNKS]
        base_mesh_paths = {
            _repo_path_key(getattr(chunk, "mesh", None))
            for chunk in getattr(new_mesh, "chunks", None) or []
            if getattr(chunk, "mesh", None)
        }
        for template_chunk, appearance, current_app in pending_w2_appearances:
            cooked_template = _build_w2_cooked_appearance_template(
                file,
                template_chunk,
                appearance,
                current_app,
                search_chunks,
                base_mesh_paths,
            )
            if cooked_template:
                setattr(current_app, "includedTemplates", [cooked_template])
            else:
                log.debug(
                    "Witcher 2 cooked appearance had no resolved chunks: appearance=%s parts=%s head=%s",
                    getattr(current_app, "name", ""),
                    _extract_cname_array_values(_find_prop_by_name(appearance, "parts")),
                    _prop_to_string(_find_prop_by_name(appearance, "headName")),
                )

    if file.HEADER.version <= 115:
        for _depot_path, related_entity in _iter_related_w2_entities():
            if not hasCMovingPhysicalAgentComponent and getattr(related_entity, "MovingPhysicalAgentComponent", None):
                this_Entity.MovingPhysicalAgentComponent = copy.deepcopy(related_entity.MovingPhysicalAgentComponent)
                hasCMovingPhysicalAgentComponent = True
            _merge_related_appearances(related_entity)
            if not this_Entity.slots and getattr(related_entity, "slots", None):
                this_Entity.slots = copy.deepcopy(related_entity.slots)
            if getattr(related_entity, "inventoryDefinitions", None):
                current_defs = getattr(this_Entity, "inventoryDefinitions", [])
                this_Entity.inventoryDefinitions = _merge_related_inventory_definitions(current_defs, related_entity.inventoryDefinitions)
            if not getattr(new_mesh, "chunks", None) and getattr(getattr(related_entity, "staticMeshes", None), "chunks", None):
                new_mesh.chunks = copy.deepcopy(related_entity.staticMeshes.chunks)

    _merge_inherited_coloring_entries()

    if not hasCMovingPhysicalAgentComponent:
        for ent in new_mesh.chunks:
            if ent.type == "CAnimatedComponent":
                this_Entity.MovingPhysicalAgentComponent = ent
                break
    this_Entity.staticMeshes = new_mesh
    return this_Entity

def load_bin_entity(fileName) -> w3_types.Entity:
    with open(fileName,"rb") as f:
        theFile = getCR2W(f)
        f.close()
        CEntity = create_CEntity(theFile)
        CEntity.version = theFile.HEADER.version
    return CEntity
