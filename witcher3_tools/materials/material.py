# Originally based on material code from Mets3D.
# https://github.com/Mets3D/batch_import_witcher3_fbx

import logging
log = logging.getLogger(__name__)

from pathlib import Path
import hashlib
import struct
import time
import tempfile
from ..CR2W import CR2W_reader
import bpy, os
from typing import Any, List, Dict, Optional, Set, Tuple
from bpy.types import Image, Material, Object, Node

from xml.etree import ElementTree
Element = ElementTree.Element

from .constants import *
from .chain import (
    chain_color_for_index,
    coerce_source_index,
    mark_imported_params_as_local,
    material_local_mode_matches,
)
from .vector_param import (
    VECTOR_PARAM_KIND,
    VECTOR_SOURCE_XYZ,
    get_mapping_vector_input,
    get_legacy_w_value,
    get_vector_node_values,
    get_vector_w,
    mark_vector_param_node,
)
from .reader import (
    _read_material_params_from_bin,
    collect_material_chain,
    normalize_depot_path,
    prune_unsupported_instance_params,
    read_instance_params,
    read_local_material_params_from_bin,
    read_material_params_from_path,
    resolve_w2mg,
)
from .w2_compat import (
    get_or_create_w2_bespoke_node_group,
    is_w2_srgb_texture_param,
    resolve_w2_bespoke_group_name,
)
from .. import get_modded_texture_path, get_uncook_path, get_mod_directory, get_tex_ext, get_texture_path
from ..extension_paths import get_texture_root, get_redkit_working_root
from ..CR2W.texture_converters import (
    convert_texarray_to_dds,
    convert_xbm_to_dds,
)
from ..CR2W.texture_dds import dds_format_name_from_header
from ..ui.blender_fun import (
    load_image_with_dds_repair,
    load_w2cube_image,
    load_w2cube_blick_equirect_image,
)
from ..CR2W.common_blender import (
    repo_file,
    win_safe_path,
    win_bpy_image_path,
    bpy_image_load_safe,
    win_path_key,
    win_unprefix_path,
    overwrite_existing_enabled,
    _active_redkit_repo_roots,
    _get_redkit_depot_roots,
    _is_under_root,
)

possible_folders = [
    'files\\Raw\\Mod',
    'files\\Raw\\DLC',
]

tex_types = [
    '.tga',
    '.dds',
    '.png'
]

_MATERIAL_PROFILE_ENABLED = True
_MATERIAL_PROFILE_WARN_THRESHOLD = 0.25
_TEXTURE_PROFILE_WARN_THRESHOLD = 0.10
_MISSING_W2_TEXTURE_LOG_KEYS: Set[str] = set()


def _log_material_profile_warning(message, *args):
    if not _MATERIAL_PROFILE_ENABLED:
        return
    log.info("[material-profile] " + str(message), *args)


def _strip_invalid_xml_chars(value) -> str:
    if value is None:
        return ""
    text = str(value)
    return "".join(
        ch for ch in text
        if ch in ("\t", "\n", "\r") or (0x20 <= ord(ch) <= 0xD7FF) or (0xE000 <= ord(ch) <= 0xFFFD)
    )


def _sanitize_xml_attr(value, fallback: str = "") -> str:
    clean = _strip_invalid_xml_chars(value).strip()
    return clean if clean else fallback


def _repo_path_from_material_file(file_path: str) -> str:
    file_path = win_unprefix_path(str(file_path or ""))
    if not file_path:
        return ""
    if not os.path.isabs(file_path):
        return normalize_depot_path(file_path)

    try:
        abs_path = os.path.normcase(os.path.normpath(os.path.realpath(file_path)))
        candidate_roots = [
            get_texture_path(bpy.context),
            get_uncook_path(bpy.context),
            get_modded_texture_path(bpy.context),
            get_mod_directory(bpy.context),
        ]
        for root in candidate_roots:
            if not root:
                continue
            root_path = os.path.normcase(os.path.normpath(os.path.realpath(win_unprefix_path(root))))
            if abs_path == root_path:
                return ""
            root_prefix = root_path + os.sep
            if abs_path.startswith(root_prefix):
                rel_path = os.path.relpath(file_path, root)
                return normalize_depot_path(rel_path)
    except Exception:
        pass
    return ""


def _material_bin_source_repo_path(mat_bin) -> str:
    depot_path = str(getattr(mat_bin, "DepotPath", "") or "")
    if depot_path.lower().endswith((".w2mi", ".w2mg")):
        return normalize_depot_path(depot_path)

    source_file = (
        getattr(mat_bin, "_W_CLASS__CR2WFILE", None)
        or getattr(mat_bin, "_CLASS__CR2WFILE", None)
    )
    file_name = str(getattr(source_file, "fileName", "") or "")
    if file_name.lower().endswith((".w2mi", ".w2mg")):
        return _repo_path_from_material_file(file_name)
    return ""


def _apply_image_texture_metadata(image: Optional[Image], source_path: str) -> None:
    if image is None or not source_path:
        return

    try:
        from ..ui.ui_texture_export import apply_texture_image_metadata

        apply_texture_image_metadata(bpy.context, image, source_path)
    except Exception:
        log.exception("Failed to seed image metadata from texture source: %s", source_path)


def repo_file_mat(filepath: str):
    if filepath.endswith(get_tex_ext(bpy.context)):
        modded_texture = os.path.join(get_modded_texture_path(bpy.context), filepath)
        if os.path.exists(modded_texture):
            return modded_texture
        else:
            for folder in possible_folders:
                modded_texture = os.path.join(get_mod_directory(bpy.context)+'\\'+folder, filepath)
                if os.path.exists(modded_texture):
                   return modded_texture
    # if filepath.endswith('.tga'):
    #     changed_filepath = filepath.replace(".tga", get_tex_ext(bpy.context))
    # elif filepath.endswith('.dds'):
    #     changed_filepath = filepath.replace(".dds", get_tex_ext(bpy.context))
    # elif filepath.endswith('.png'):
    #     changed_filepath = filepath.replace(".png", get_tex_ext(bpy.context))
    
    return filepath

def hide_unused_sockets(node, inp=True, out=True):
    if inp:
        for socket in node.inputs:
            socket.hide = True		# Blender will prevent it if it's used, no need for us to check.
    if out:
        for socket in node.outputs:
            socket.hide = True

def ensure_node_group(ng_name, resource_path=RES_PATH):
    """Check if a nodegroup exists, and if not, append it from the addon's resource file."""

    resource_path = resource_path or RES_PATH
    ng_name = _find_available_node_group_name(ng_name, resource_path) or ng_name
    if ng_name not in bpy.data.node_groups:
        with bpy.data.libraries.load(resource_path, relative=False) as (data_from, data_to):
            for ng in data_from.node_groups:
                if ng == ng_name:
                    data_to.node_groups.append(ng)

    ng = bpy.data.node_groups.get(ng_name)
    if ng is None:
        raise KeyError(f"Node group {ng_name} not found in {resource_path}")
    ng.use_fake_user = False

    family_name = _node_group_family_name(ng.name).lower()
    if family_name == 'witcher3_skin':
        _ensure_witcher3_skin_subsurface(ng)
    elif family_name == 'witcher3_eye':
        _ensure_witcher3_eye_shader(ng)

    return ng


MATERIAL_SETUP_VERSION = 7
_BASE_PATH_NODE_GROUP_CACHE: Dict[Tuple[object, ...], Dict[str, str]] = {}
_RESOURCE_NODE_GROUP_CACHE: Dict[str, Tuple[Optional[float], Set[str]]] = {}


def _resource_cache_key(resource_path: str) -> str:
    try:
        return os.path.normcase(os.path.normpath(os.path.realpath(resource_path or RES_PATH)))
    except Exception:
        return str(resource_path or RES_PATH)


def _resource_mtime(resource_path: str) -> Optional[float]:
    try:
        return os.path.getmtime(resource_path or RES_PATH)
    except OSError:
        return None


def _resource_node_group_names(resource_path: str = RES_PATH) -> Set[str]:
    resource_path = resource_path or RES_PATH
    cache_key = _resource_cache_key(resource_path)
    mtime = _resource_mtime(resource_path)
    cached = _RESOURCE_NODE_GROUP_CACHE.get(cache_key)
    if cached and cached[0] == mtime:
        return set(cached[1])

    node_group_names: Set[str] = set()
    try:
        with bpy.data.libraries.load(resource_path, relative=False) as (data_from, _data_to):
            node_group_names = set(data_from.node_groups or [])
    except Exception:
        log.exception("Failed to inspect material node groups from %s", resource_path)

    _RESOURCE_NODE_GROUP_CACHE[cache_key] = (mtime, node_group_names)
    return set(node_group_names)


def _find_available_node_group_name(ng_name: str, resource_path: str = RES_PATH) -> str:
    ng_name = str(ng_name or "")
    if not ng_name:
        return ""

    if bpy.data.node_groups.get(ng_name) is not None:
        return ng_name

    target_lower = ng_name.lower()
    try:
        loaded_names = list(bpy.data.node_groups.keys())
    except Exception:
        loaded_names = [node_group.name for node_group in bpy.data.node_groups]
    for loaded_name in loaded_names:
        if str(loaded_name).lower() == target_lower:
            return str(loaded_name)

    resource_names = _resource_node_group_names(resource_path)
    if ng_name in resource_names:
        return ng_name
    for resource_name in sorted(resource_names):
        if resource_name.lower() == target_lower:
            return resource_name
    return ""


def resolve_material_node_group_name(
        shader_type: str,
        shader_mapping: Dict[str, str],
        fallback_ng_name: str,
        resource_path: str = RES_PATH,
        ) -> str:
    exact_ng_name = _find_available_node_group_name(shader_type, resource_path)
    if exact_ng_name:
        return exact_ng_name
    return shader_mapping.get(shader_type, fallback_ng_name)


def _node_group_family_name(ng_name: str) -> str:
    ng_name = str(ng_name or "")
    if len(ng_name) > 4 and ng_name[-4] == "." and ng_name[-3:].isdigit():
        return ng_name[:-4]
    return ng_name


def _node_group_names_match(actual_name: str, expected_name: str) -> bool:
    if not expected_name:
        return True
    return _node_group_family_name(actual_name).lower() == _node_group_family_name(expected_name).lower()


def is_witcher2_material(material: Material) -> bool:
    props = getattr(material, "witcher_props", None)
    return bool(props and props.material_version == 'witcher2')


def _material_repo_version(material: Material) -> int:
    return 115 if is_witcher2_material(material) else 999


def _material_bin_cr2w_version(mat_bin) -> int:
    try:
        return int(mat_bin.get_CR2W_version())
    except Exception:
        pass

    source_file = (
        getattr(mat_bin, "_W_CLASS__CR2WFILE", None)
        or getattr(mat_bin, "_CLASS__CR2WFILE", None)
    )
    header = getattr(source_file, "HEADER", None)
    try:
        return int(getattr(header, "version", 999))
    except Exception:
        return 999


def _material_bin_is_witcher2(mat_bin) -> bool:
    return _material_bin_cr2w_version(mat_bin) <= 115


def _texture_xbm_repo_path(texture_path: str) -> str:
    texture_path = str(texture_path or "").strip()
    if not texture_path:
        return ""
    root, ext = os.path.splitext(texture_path)
    if ext.lower() in {".dds", ".tga", ".png", ".xbm"}:
        return root + ".xbm"
    return texture_path


def _resolve_existing_texture_xbm(texture_path: str, repo_version: int) -> str:
    xbm_repo_path = _texture_xbm_repo_path(texture_path)
    if not xbm_repo_path:
        return ""
    try:
        resolved_path = repo_file(xbm_repo_path, version=repo_version)
    except Exception:
        resolved_path = ""
        log.debug("Failed to resolve texture XBM through repo_file: %s", xbm_repo_path, exc_info=True)
    if resolved_path and os.path.exists(win_safe_path(resolved_path)):
        return resolved_path
    if repo_version <= 115:
        log_key = normalize_depot_path(xbm_repo_path)
        if log_key not in _MISSING_W2_TEXTURE_LOG_KEYS:
            _MISSING_W2_TEXTURE_LOG_KEYS.add(log_key)
            log.critical(
                "Witcher 2 texture was not found or extracted from bundles: %s -> %s",
                xbm_repo_path,
                resolved_path or "<unresolved>",
            )
    return ""


def _source_file_cache_identity(path_value: str) -> Tuple[str, int, int]:
    source_path = os.path.abspath(win_safe_path(str(path_value or "")))
    try:
        stat = os.stat(source_path)
        mtime_ns = int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1000000000)))
        size = int(stat.st_size)
    except OSError:
        mtime_ns = 0
        size = 0
    return source_path, mtime_ns, size


def _derived_file_is_current(output_path: str, source_path: str) -> bool:
    if not output_path or not os.path.exists(win_safe_path(output_path)):
        return False
    try:
        source_mtime = os.path.getmtime(win_safe_path(source_path))
        output_mtime = os.path.getmtime(win_safe_path(output_path))
    except OSError:
        return False
    return output_mtime >= source_mtime


def _redkit_source_roots() -> List[str]:
    roots = []
    try:
        roots.extend(_active_redkit_repo_roots())
    except Exception:
        pass
    try:
        roots.extend(_get_redkit_depot_roots())
    except Exception:
        pass
    seen = set()
    result = []
    for root in roots:
        key = os.path.normcase(os.path.normpath(str(root or "")))
        if key and key not in seen:
            seen.add(key)
            result.append(str(root))
    return result


def _is_redkit_source_path(path_value: str) -> bool:
    if not path_value:
        return False
    path = win_unprefix_path(str(path_value))
    for root in _redkit_source_roots():
        if _is_under_root(path, root):
            return True
    return False


def _safe_cache_file_stem(value: str) -> str:
    stem = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(value or "texture"))
    stem = stem.strip("._") or "texture"
    return stem[:80]


def _cached_redkit_dds_image_path(dds_path: str, extension: str) -> str:
    source_path, mtime_ns, size = _source_file_cache_identity(dds_path)
    key = f"{source_path.lower()}|{mtime_ns}|{size}|{extension.lower()}"
    digest = hashlib.sha1(key.encode("utf-8", errors="ignore")).hexdigest()
    stem = _safe_cache_file_stem(Path(source_path).stem)
    try:
        texture_root = os.path.join(get_redkit_working_root(True), "_converted_textures")
    except OSError:
        texture_root = os.path.join(tempfile.gettempdir(), "witcher3_tools", "witcher_redkit_working", "_converted_textures")
    cache_dir = os.path.join(texture_root, "converted_dds", digest[:2])
    try:
        os.makedirs(win_safe_path(cache_dir), exist_ok=True)
    except OSError:
        cache_dir = os.path.join(tempfile.gettempdir(), "witcher3_tools", "witcher_redkit_working", "_converted_textures", "converted_dds", digest[:2])
        os.makedirs(win_safe_path(cache_dir), exist_ok=True)
    extension = extension if extension.startswith(".") else f".{extension}"
    return os.path.join(cache_dir, f"{stem}_{digest[:16]}{extension.lower()}")


def _derived_image_is_current(output_path: str, source_path: str) -> bool:
    if not _derived_file_is_current(output_path, source_path):
        return False
    try:
        return os.path.getsize(win_safe_path(output_path)) > 0
    except OSError:
        return False


def _read_dds_blender_cache_info(dds_path: str) -> Optional[Tuple[bytes, Optional[int], str]]:
    try:
        with open(win_safe_path(dds_path), "rb") as handle:
            header = handle.read(148)
    except OSError:
        return None

    if len(header) < 128 or header[:4] != b"DDS ":
        return None

    fourcc = header[84:88]
    dxgi_format = None
    if fourcc == b"DX10" and len(header) >= 132:
        try:
            dxgi_format = struct.unpack_from("<I", header, 128)[0]
        except struct.error:
            dxgi_format = None

    format_name = ""
    try:
        format_name, _width, _height, _mip_count, _data_offset = dds_format_name_from_header(header)
    except Exception:
        # A DX10 header is enough to know Blender's legacy DDS path may reject
        # the file, even if our lightweight parser does not know the DXGI enum.
        format_name = ""

    return fourcc, dxgi_format, format_name


def _dds_needs_blender_image_cache(dds_path: str) -> bool:
    if not dds_path or not str(dds_path).lower().endswith(".dds"):
        return False
    if not os.path.isfile(win_safe_path(dds_path)):
        return False
    info = _read_dds_blender_cache_info(dds_path)
    if not info:
        return False
    fourcc, _dxgi_format, _format_name = info
    return fourcc == b"DX10"


def _replace_converted_cache_output(converted_path: str, output_path: str) -> str:
    converted_path = win_unprefix_path(converted_path or "")
    output_path = win_unprefix_path(output_path or "")
    if not converted_path or not output_path or not os.path.isfile(win_safe_path(converted_path)):
        return ""
    if win_path_key(converted_path) != win_path_key(output_path):
        os.makedirs(win_safe_path(os.path.dirname(output_path)), exist_ok=True)
        os.replace(win_safe_path(converted_path), win_safe_path(output_path))
    return output_path if os.path.isfile(win_safe_path(output_path)) else ""


def _save_cached_tga_as_png(tga_path: str, png_path: str) -> str:
    temp_image = None
    try:
        temp_image = bpy_image_load_safe(tga_path, check_existing=False)
        if temp_image is None:
            return ""
        temp_image.filepath_raw = win_bpy_image_path(png_path)
        temp_image.file_format = 'PNG'
        temp_image.save()
        if os.path.isfile(win_safe_path(png_path)) and os.path.getsize(win_safe_path(png_path)) > 0:
            return png_path
    except Exception:
        log.debug("Failed to re-save converted DDS TGA as PNG: %s -> %s", tga_path, png_path, exc_info=True)
    finally:
        if temp_image is not None:
            try:
                bpy.data.images.remove(temp_image)
            except Exception:
                pass
    return ""


def _convert_dds_to_blender_image_cache(dds_path: str) -> str:
    """Convert DX10 DDS variants that Blender cannot decode to a cached image."""
    if not _dds_needs_blender_image_cache(dds_path):
        return ""

    png_path = _cached_redkit_dds_image_path(dds_path, ".png")
    if _derived_image_is_current(png_path, dds_path):
        return png_path

    tga_path = _cached_redkit_dds_image_path(dds_path, ".tga")
    if _derived_image_is_current(tga_path, dds_path):
        png_from_tga = _save_cached_tga_as_png(tga_path, png_path)
        return png_from_tga or tga_path

    cache_dir = os.path.dirname(png_path)
    info = _read_dds_blender_cache_info(dds_path)
    format_label = ""
    if info:
        _fourcc, dxgi_format, format_name = info
        format_label = format_name or (f"DXGI {dxgi_format}" if dxgi_format is not None else "DX10")

    try:
        from ..CR2W import texconv_wrapper
    except Exception:
        log.warning("Cannot convert Blender-incompatible DDS because texconv is unavailable: %s", dds_path, exc_info=True)
        return ""

    png_error = None
    try:
        converted_png = texconv_wrapper.convert_dds_to_png(dds_path, output_dir=cache_dir)
        converted_png = _replace_converted_cache_output(converted_png, png_path)
        if _derived_image_is_current(converted_png, dds_path):
            log.info("Converted Blender-incompatible DDS to PNG cache (%s): %s -> %s", format_label, dds_path, converted_png)
            return converted_png
    except Exception as exc:
        png_error = exc
        log.debug("DDS to PNG conversion failed, trying TGA fallback: %s", dds_path, exc_info=True)

    try:
        converted_tga = texconv_wrapper.convert_dds_to_tga(dds_path, output_dir=cache_dir)
        converted_tga = _replace_converted_cache_output(converted_tga, tga_path)
        png_from_tga = _save_cached_tga_as_png(converted_tga, png_path)
        if _derived_image_is_current(png_from_tga, dds_path):
            log.info("Converted Blender-incompatible DDS to PNG cache via TGA (%s): %s -> %s", format_label, dds_path, png_from_tga)
            return png_from_tga
        if _derived_image_is_current(converted_tga, dds_path):
            if png_error is not None:
                log.warning(
                    "DDS to PNG conversion failed for %s; loading TGA cache instead: %s (%s)",
                    dds_path,
                    converted_tga,
                    png_error,
                )
            return converted_tga
    except Exception as exc:
        log.warning("Failed to convert Blender-incompatible DDS for material preview: %s (%s)", dds_path, exc)
    return ""


def _cached_dds_path_for_xbm(xbm_path: str, use_redkit_working_root: bool = False) -> str:
    source_path, mtime_ns, size = _source_file_cache_identity(xbm_path)
    key = f"{source_path.lower()}|{mtime_ns}|{size}"
    digest = hashlib.sha1(key.encode("utf-8", errors="ignore")).hexdigest()
    stem = Path(source_path).stem or "texture"
    try:
        if use_redkit_working_root:
            texture_root = os.path.join(get_redkit_working_root(True), "_converted_textures")
        else:
            texture_root = get_texture_root(True)
    except OSError:
        texture_root = os.path.join(tempfile.gettempdir(), "witcher3_tools", "witcher_textures")
    cache_dir = os.path.join(texture_root, "converted_xbm", digest[:2])
    try:
        os.makedirs(cache_dir, exist_ok=True)
    except OSError:
        cache_dir = os.path.join(tempfile.gettempdir(), "witcher3_tools", "witcher_textures", "converted_xbm", digest[:2])
        os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, f"{stem}_{digest[:16]}.dds")


def _convert_xbm_to_writable_dds(xbm_path: str, preferred_dds_path: str = "") -> str:
    preferred_dds_path = preferred_dds_path or (os.path.splitext(xbm_path)[0] + ".dds")
    redkit_source = _is_redkit_source_path(xbm_path)
    preferred_allowed = preferred_dds_path and not _is_redkit_source_path(preferred_dds_path)
    if preferred_allowed and _derived_file_is_current(preferred_dds_path, xbm_path):
        return preferred_dds_path
    if preferred_allowed:
        try:
            converted_path = convert_xbm_to_dds(xbm_path, out_path=preferred_dds_path)
            if converted_path and _derived_file_is_current(converted_path, xbm_path):
                return converted_path
            if _derived_file_is_current(preferred_dds_path, xbm_path):
                return preferred_dds_path
        except OSError:
            log.debug("Could not write preferred DDS, using texture cache: %s", xbm_path, exc_info=True)

    cached_dds_path = _cached_dds_path_for_xbm(
        xbm_path,
        use_redkit_working_root=redkit_source,
    )
    if _derived_file_is_current(cached_dds_path, xbm_path):
        return cached_dds_path
    converted_path = convert_xbm_to_dds(xbm_path, out_path=cached_dds_path)
    if converted_path and _derived_file_is_current(converted_path, xbm_path):
        return converted_path
    if _derived_file_is_current(cached_dds_path, xbm_path):
        return cached_dds_path
    return ""


def _local_handle_ref_chunk(source_chunk, handle):
    if not source_chunk or not handle or not getattr(handle, "ChunkHandle", False):
        return None
    cr2w_file = getattr(source_chunk, "_W_CLASS__CR2WFILE", None)
    ref_idx = getattr(handle, "Reference", None)
    if not cr2w_file or not isinstance(ref_idx, int):
        return None
    chunks = getattr(getattr(cr2w_file, "CHUNKS", None), "CHUNKS", None) or []
    if 0 <= ref_idx < len(chunks):
        return chunks[ref_idx]
    return None


def _attach_local_material_graph_params(graph_chunk):
    if not graph_chunk or getattr(graph_chunk, "Type", None) != "CMaterialGraph":
        return None
    if getattr(graph_chunk, "_graph_params", None) is not None:
        return graph_chunk
    parameter_blocks = graph_chunk.GetVariableByName('parameterBlocks')
    graph_params = []
    for handle in getattr(parameter_blocks, "Handles", None) or []:
        param_chunk = _local_handle_ref_chunk(graph_chunk, handle)
        if getattr(param_chunk, "Type", "").startswith("CMaterialParameter"):
            graph_params.append(param_chunk)
    graph_chunk._graph_params = graph_params
    return graph_chunk


def _guess_w2_local_material_base(mat_bin, material_name: str = "") -> str:
    # Stopgap keyword heuristic for W2 materials that ship with an inline CMaterialGraph
    # instead of a baseMaterial handle. Picks a plausible .w2mg from the material/file
    # name and parameter names. Replace once we parse the embedded graph for real.
    mat_instance = getattr(mat_bin, 'CMaterialInstance', None)
    elements = getattr(getattr(mat_instance, 'InstanceParameters', None), 'elements', []) or []
    param_names = set()
    texture_paths = []
    for mat_param in elements:
        prop = getattr(mat_param, "PROP", None)
        if not prop:
            continue
        prop_name = str(getattr(prop, "theName", "") or "")
        if prop_name:
            param_names.add(prop_name.lower())
        for handle in getattr(prop, "Handles", []) or []:
            depot_path = getattr(handle, "DepotPath", None)
            if depot_path:
                texture_paths.append(str(depot_path).lower())

    name_hint = str(material_name or "").lower()
    source_file = getattr(mat_bin, "_W_CLASS__CR2WFILE", None) or getattr(mat_bin, "_CLASS__CR2WFILE", None)
    file_hint = str(getattr(source_file, "fileName", "") or "").lower()
    depot_hint = str(getattr(mat_bin, "DepotPath", "") or "").lower()
    combined_hint = f"{name_hint} {file_hint} {depot_hint}"
    if "hair" in combined_hint or "coif" in combined_hint:
        return r"characters\shaders\hair.w2mg"
    if "eye" in combined_hint and "eyelash" not in combined_hint:
        return r"characters\shaders\eye_witcher.w2mg"
    if any(word in combined_hint for word in ("skin", "body", "head", "hand", "face")):
        return r"characters\shaders\skin.w2mg"
    if any(word in combined_hint for word in ("metal", "steel", "silver", "sword", "scabbard", "medalion", "medallion")):
        return r"characters\shaders\metal.w2mg"
    if "glass" in combined_hint:
        return r"characters\shaders\glass.w2mg"
    if (
        "tex_specshift" in param_names
        or any("scattering" in name for name in param_names)
        or any("translucency" in name for name in param_names)
    ):
        return r"characters\shaders\hair.w2mg"
    if any("sheet" in path for path in texture_paths):
        return DEFAULT_W2_MATERIAL_BASE
    return DEFAULT_W2_MATERIAL_BASE


def resolve_witcher2_shader_type(mat_base: str, shader_type: str) -> str:
    normalized_base = normalize_depot_path(mat_base)
    direct_match = WITCHER2_SHADER_BY_BASE_PATH.get(normalized_base)
    if not direct_match:
        for base_path, mapped_shader in WITCHER2_SHADER_BY_BASE_PATH.items():
            if normalized_base.endswith(base_path):
                direct_match = mapped_shader
                break
    if direct_match:
        return direct_match

    if shader_type in SHADER_MAPPING_W2:
        return shader_type

    guessed_shader = guess_shader_type(shader_type)
    log.info(f"Witcher 2 shader fallback: {shader_type} -> {guessed_shader}")
    return guessed_shader


def get_shader_resources_for_material(material: Material):
    if is_witcher2_material(material):
        return SHADER_MAPPING_W2, 'Witcher2_Main', RES_PATH
    return SHADER_MAPPING, 'Witcher3_Main', RES_PATH


def _shader_name_from_material_path(material_path: str) -> str:
    normalized_path = normalize_depot_path(material_path)
    if not normalized_path:
        return ""
    return os.path.splitext(normalized_path.rsplit("\\", 1)[-1])[0]


def get_recommended_node_group_for_base_path(material: Material, material_path: str) -> Dict[str, str]:
    normalized_path = normalize_depot_path(material_path)
    shader_mapping, fallback_ng_name, resource_path = get_shader_resources_for_material(material)
    material_version = 'witcher2' if is_witcher2_material(material) else 'witcher3'
    resource_stamp = _resource_mtime(resource_path)
    cache_key = (material_version, normalized_path, resource_stamp)

    if cache_key in _BASE_PATH_NODE_GROUP_CACHE:
        cached = dict(_BASE_PATH_NODE_GROUP_CACHE[cache_key])
        cached["requested_path"] = material_path or ""
        cached["node_group_name"] = resolve_material_node_group_name(
            cached.get("shader_type", ""),
            shader_mapping,
            fallback_ng_name,
            cached.get("resource_path") or resource_path,
        )
        if cached.get("w2_bespoke_node_group_name"):
            cached["w2_base_node_group_name"] = cached["node_group_name"]
            cached["node_group_name"] = cached["w2_bespoke_node_group_name"]
        return cached

    result = {
        "requested_path": material_path or "",
        "normalized_path": normalized_path,
        "resolved_path": normalized_path,
        "shader_type": "",
        "node_group_name": "",
        "w2_base_node_group_name": "",
        "w2_bespoke_node_group_name": "",
        "resource_path": resource_path,
    }
    if not normalized_path:
        return result

    shader_type = _shader_name_from_material_path(normalized_path)
    resolved_path = normalized_path

    if normalized_path.endswith(".w2mi"):
        fallback_shader_type = guess_shader_type(shader_type)
        resolved_w2mg = resolve_w2mg(normalized_path, version=_material_repo_version(material))
        if resolved_w2mg:
            resolved_path = normalize_depot_path(resolved_w2mg)
            shader_type = _shader_name_from_material_path(resolved_path)
        else:
            shader_type = fallback_shader_type

    if is_witcher2_material(material):
        shader_type = resolve_witcher2_shader_type(resolved_path, shader_type)

    result["resolved_path"] = resolved_path
    result["shader_type"] = shader_type
    result["node_group_name"] = resolve_material_node_group_name(
        shader_type,
        shader_mapping,
        fallback_ng_name,
        resource_path,
    )
    if is_witcher2_material(material):
        bespoke_ng_name = resolve_w2_bespoke_group_name(resolved_path, version=_material_repo_version(material))
        if bespoke_ng_name:
            result["w2_bespoke_node_group_name"] = bespoke_ng_name
            result["w2_base_node_group_name"] = result["node_group_name"]
            result["node_group_name"] = bespoke_ng_name

    _BASE_PATH_NODE_GROUP_CACHE[cache_key] = dict(result)
    return result


def ensure_node_group_for_recommendation(recommendation):
    """Materialize the recommended node group.

    Bespoke W2 groups don't live in the resource .blend, so they are copied
    from their base group on demand; everything else goes through
    ensure_node_group directly.
    """
    recommendation = recommendation or {}
    resource_path = recommendation.get("resource_path") or RES_PATH
    base_ng_name = recommendation.get("w2_base_node_group_name") or recommendation.get("node_group_name", "")
    ng = ensure_node_group(base_ng_name, resource_path=resource_path)
    if recommendation.get("w2_bespoke_node_group_name"):
        bespoke = get_or_create_w2_bespoke_node_group(ng, recommendation.get("resolved_path", ""))
        if bespoke is not None:
            return bespoke
    return ng


def load_w3_materials_XML(
        obj: Object
        ,uncook_path: str
        ,xml_path: str
        ,force_mat_update = False
    ):
    """Read XML data and sets up all materials on the object.
    This unavoidable requires that the materials were not renamed
    after the FBX import in any way, including any .001 shennanigans.
    """
    xml_started = time.perf_counter()
    root: Element = readXML(xml_path)

    for root_element in root:
        if root_element.tag == 'materials':
            for xml_data in root_element:
                material_started = time.perf_counter()
                xml_mat_name = xml_data.get('name')
                if xml_mat_name == "":
                    log.info("No material name? " + obj.name)
                    continue
                # Find corresponding blender material.
                target_mat = None
                for mat in obj.data.materials:
                    if not mat:
                        # Idk how, but this happens.
                        continue
                    if "Material" not in mat.name:
                        # This material was already processed.
                        continue
                    #remove any images the model imported so it doesn't conflict with repo import
                    for node in mat.node_tree.nodes:
                        if node.type == "TEX_IMAGE"and node.image:
                            bpy.data.images.remove(node.image)
                        mat.node_tree.nodes.remove( node )
                    #mat.node_tree.asset_clear()
                    
                    # Compare the number at the end of the blender material name "MaterialX"
                    # to the last character of the XML material.
                    material_number = mat.name.split("Material")[1]
                    assert mat.name[-4] != ".", f"ERROR: Material {mat.name} has .00x suffix. This must be avoided!"
                    xml_material_number = xml_mat_name.split("Material")[1]
                    if "Material" in mat.name and material_number == xml_material_number:
                        target_mat = mat
                        break
                if not target_mat:
                    # Didn't find a matching blender material.
                    # Must be a material that's only for LODs, so let's ignore.
                    continue
                finished_mat = setup_w3_material(uncook_path, target_mat, xml_data, xml_path, force_update=force_mat_update)
                obj.material_slots[target_mat.name].material = finished_mat
                material_seconds = time.perf_counter() - material_started
                if material_seconds >= _MATERIAL_PROFILE_WARN_THRESHOLD:
                    _log_material_profile_warning(
                        "xml material %s on %s total %.3fs (xml %s)",
                        xml_mat_name,
                        obj.name,
                        material_seconds,
                        os.path.basename(xml_path),
                    )
    total_seconds = time.perf_counter() - xml_started
    if total_seconds >= _MATERIAL_PROFILE_WARN_THRESHOLD:
        _log_material_profile_warning(
            "xml material batch %s total %.3fs (object %s)",
            os.path.basename(xml_path),
            total_seconds,
            obj.name,
        )

def find_mapping_nodes(node_tree):
    mapping_nodes = []
    for node in node_tree.nodes:
        if node.bl_idname == 'ShaderNodeMapping':
            mapping_nodes.append(node)
    return mapping_nodes

def readXML(xml_path) -> Element:
    """Read Witcher 3 material info read from an .xml file, and return the root Element."""
    try:
        with open(xml_path, 'r') as myFile:
            # XXX: Parsing the file directly doesn't work due to a bug in ElementTree
            # that rejects UTF-16, so we have to use fromstring().
            data = myFile.read()
    except Exception:
        with open(xml_path, 'r', encoding='utf-16-le') as myFile:
            # XXX: Parsing the file directly doesn't work due to a bug in ElementTree
            # that rejects UTF-16, so we have to use fromstring().
            data = myFile.read()
    return ElementTree.fromstring(data)

def _ngt_new_input(ngt, socket_type, name):
    """Add an input socket to a node group tree, compatible with Blender 4.0+."""
    if bpy.app.version >= (4, 0, 0):
        ngt.interface.new_socket(name=name, in_out='INPUT', socket_type=socket_type)
    else:
        ngt.inputs.new(socket_type, name)

def _ngt_new_output(ngt, socket_type, name):
    """Add an output socket to a node group tree, compatible with Blender 4.0+."""
    if bpy.app.version >= (4, 0, 0):
        ngt.interface.new_socket(name=name, in_out='OUTPUT', socket_type=socket_type)
    else:
        ngt.outputs.new(socket_type, name)

def _new_mix_color_node(node_tree):
    """Create a color mix node compatible with Blender 4.0+."""
    if bpy.app.version >= (4, 0, 0):
        node = node_tree.nodes.new('ShaderNodeMix')
        node.data_type = 'RGBA'
        node.blend_type = 'MIX'
    else:
        node = node_tree.nodes.new('ShaderNodeMixRGB')
        node.blend_type = 'MIX'
    return node

def _mix_fac_input(mix_node):
    """Return the factor input index for a mix node."""
    if bpy.app.version >= (4, 0, 0):
        return mix_node.inputs[0]  # "Factor" at index 0
    return mix_node.inputs[0]  # "Fac" at index 0

def _mix_color_inputs(mix_node):
    """Return (color_A_input, color_B_input) for a mix node."""
    if bpy.app.version >= (4, 0, 0):
        return mix_node.inputs[6], mix_node.inputs[7]  # A, B for RGBA
    return mix_node.inputs[1], mix_node.inputs[2]  # Color1, Color2

def _mix_color_output(mix_node):
    """Return the color output socket for a mix node."""
    if bpy.app.version >= (4, 0, 0):
        return mix_node.outputs[2]  # Result (RGBA)
    return mix_node.outputs[0]  # Color


def _sanitize_node_name_part(value: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in str(value or ""))


def _internal_helper_node_name(kind: str, *parts: str) -> str:
    safe_parts = [_sanitize_node_name_part(part) for part in parts if part]
    joined = "_".join(part for part in safe_parts if part)
    return f"__W3_{kind}_{joined}" if joined else f"__W3_{kind}"

def create_instance_group(  material,
                            xml_data,
                            xml_path,
                            mat_base,
                            shader_type,
                            uncook_path,
                            x_loc):
    nodes = material.node_tree.nodes
    links = material.node_tree.links

    nodegroup_node = init_instance_nodes(material, shader_type, clear = False, x_loc = x_loc)
    nodegroup_node.name = material.name
    #nodes_create_outputs(material, nodes, links, nodegroup_node, xml_data, xml_path)

    ngt = nodegroup_node.node_tree

    # create group inputs
    group_inputs = ngt.nodes.new('NodeGroupInput')
    group_inputs.location = (-550,0)
    # create group outputs
    group_outputs = ngt.nodes.new('NodeGroupOutput')
    group_outputs.location = (300,0)

    # Order parameters so input nodes get created in a specified order, from top to bottom relative to the inputs of the nodegroup.
    # Purely for neatness of the node noodles.
    ordered_params = order_elements_by_attribute(xml_data, PARAM_ORDER, 'name')


    for idx, p in enumerate(ordered_params):
        par_name = p.get('name')
        par_type = p.get('type')
        par_value = p.get('value')
        if par_type == "Color":
            _ngt_new_input(ngt, 'NodeSocketColor', par_name)
            _ngt_new_output(ngt, 'NodeSocketColor', par_name)
            values = [float(f) for f in par_value.split("; ")]
            d_val = (
                values[0] / 255
                ,values[1] / 255
                ,values[2] / 255
                ,values[3] / 255
            )
            ngt.inputs[par_name].default_value = d_val
            nodegroup_node.inputs[par_name].default_value = d_val
        elif par_type == "Float":
            _ngt_new_input(ngt, 'NodeSocketFloat', par_name)
            _ngt_new_output(ngt, 'NodeSocketFloat', par_name)
            ngt.inputs[par_name].default_value = float(par_value)
            nodegroup_node.inputs[par_name].default_value = float(par_value)

            ngt.links.new(group_inputs.outputs[par_name], group_outputs.inputs[par_name])
        elif par_type == "handle:ITexture":
            _ngt_new_input(ngt, 'NodeSocketColor', par_name)
            _ngt_new_output(ngt, 'NodeSocketColor', par_name)
            _ngt_new_input(ngt, 'NodeSocketFloat', par_name+"_active")

            # create three math nodes in a group
            mix_node_1 = _new_mix_color_node(ngt)
            mix_node_1.location = (0,0+(-500*idx))
            color_a, color_b = _mix_color_inputs(mix_node_1)
            ngt.links.new(group_inputs.outputs[par_name], color_b)
            ngt.links.new(_mix_color_output(mix_node_1), group_outputs.inputs[par_name])

            math_node_1 = ngt.nodes.new('ShaderNodeMath')
            math_node_1.location = (-320,200+(-500*idx))
            math_node_1.operation = 'GREATER_THAN'


            ngt.links.new(_mix_fac_input(mix_node_1), math_node_1.outputs[0])
            ngt.links.new(math_node_1.inputs[0], group_inputs.outputs[par_name+"_active"])

            #node = ngt.nodes.new(type="ShaderNodeTexImage")
            #node.width = 300
            node = create_node_texture(material, p, ngt, 0+(500*idx), uncook_path, 0, using_node_tree = True)

            node.location = (-320,0+(-500*idx))
            if node and node.image:
                if par_name in ['Diffuse', 'SpecularTexture', 'SnowDiffuse']:
                    node.image.colorspace_settings.name = 'sRGB'
                else:
                    node.image.colorspace_settings.name = 'Non-Color'

            if node and node.image and len(node.outputs[0].links) > 0:
                pin_name = node.outputs[0].links[0].to_socket.name
                if pin_name in ['Diffuse', 'SpecularTexture', 'SnowDiffuse']:
                    node.image.colorspace_settings.name = 'sRGB'
                else:
                    node.image.colorspace_settings.name = 'Non-Color'
            ngt.links.new(node.outputs["Color"], color_a)

        elif par_type == 'Vector':
            _ngt_new_input(ngt, 'NodeSocketVector', par_name)
            _ngt_new_output(ngt, 'NodeSocketVector', par_name)
            ngt.links.new(group_inputs.outputs[par_name], group_outputs.inputs[par_name])

            values = [float(f) for f in par_value.split("; ")]
            d_val = (
                values[0]
                ,values[1]
                ,values[2]
            )
            ngt.inputs[par_name].default_value = d_val
            nodegroup_node.inputs[par_name].default_value = d_val
        else:
            _ngt_new_input(ngt, 'NodeSocketFloat', par_name)
            _ngt_new_output(ngt, 'NodeSocketFloat', par_name)
            ngt.links.new(group_inputs.outputs[par_name], group_outputs.inputs[par_name])

    return (ordered_params, nodegroup_node)

def xml_data_from_CR2W(mat_bin, name = None):
    is_w2_material = _material_bin_is_witcher2(mat_bin)
    default_base = DEFAULT_W2_MATERIAL_BASE if is_w2_material else DEFAULT_W3_MATERIAL_BASE
    source_repo_path = _material_bin_source_repo_path(mat_bin)
    base_var = mat_bin.GetVariableByName('baseMaterial')
    if base_var:
        handle = base_var.Handles[0] if getattr(base_var, "Handles", None) else None
        mat_base = getattr(handle, "DepotPath", None) if handle else None
        if not mat_base:
            local_graph = _local_handle_ref_chunk(mat_bin, handle)
            if getattr(local_graph, "Type", None) == "CMaterialGraph":
                _attach_local_material_graph_params(local_graph)
            mat_base = _guess_w2_local_material_base(mat_bin, name) if is_w2_material else default_base
    elif hasattr(mat_bin, 'DepotPath') and mat_bin.DepotPath:
        # CMaterialGraph referenced directly (no instance wrapper) — the chunk IS the shader
        mat_base = mat_bin.DepotPath
    elif getattr(mat_bin, "Type", None) == "CMaterialGraph":
        _attach_local_material_graph_params(mat_bin)
        mat_base = _guess_w2_local_material_base(mat_bin, name) if is_w2_material else default_base
    else:
        mat_base = default_base
    shader_type = mat_base.split("\\")[-1][:-5]	# The .w2mg or .w2mi file, minus the extension.
    
    if name == None:
        filePath = mat_bin._CLASS__CR2WFILE.fileName
    
    new_xml = ElementTree.Element('material')
    new_xml.set('name', _sanitize_xml_attr(name if name else Path(filePath).stem, "material"))
    new_xml.set('local', "true")
    new_xml.set('base', _sanitize_xml_attr(mat_base, default_base))
    if source_repo_path:
        new_xml.set('source_path', _sanitize_xml_attr(source_repo_path))

    w2mi_params = {}
    read_instance_params(mat_bin, w2mi_params)
    for name, attrs in w2mi_params.items():
        create_param(
            xml_data = new_xml
            ,name = name 
            ,type = attrs[0]
            ,value = attrs[1]
            ,witcher_source_path = source_repo_path
            ,witcher_source_kind = "instance" if source_repo_path.lower().endswith(".w2mi") else "graph_default"
            ,witcher_source_index = 0 if source_repo_path else -1
        )
    prune_unsupported_instance_params(new_xml, mat_base, version=_material_bin_cr2w_version(mat_bin))
    return new_xml

def get_all_w2mi(w2mi_path, all_instances):
    full_path = repo_file(w2mi_path) #os.path.join(get_uncook_path(bpy.context), w2mi_path)
    material_bin = CR2W_reader.load_material(full_path)[0]

    xml_data = xml_data_from_CR2W(material_bin)
    mat_base = xml_data.get('base')
    all_instances.append(xml_data)

    if mat_base.endswith(".w2mi"):
        return get_all_w2mi(mat_base, all_instances)
    else:
        return mat_base


def _iter_material_node_trees(material: Material):
    if material is None:
        return
    seen = set()
    stack = [getattr(material, "node_tree", None)]
    while stack:
        node_tree = stack.pop()
        if node_tree is None:
            continue
        try:
            tree_id = int(node_tree.as_pointer())
        except Exception:
            tree_id = id(node_tree)
        if tree_id in seen:
            continue
        seen.add(tree_id)
        yield node_tree
        for node in getattr(node_tree, "nodes", []):
            child_tree = getattr(node, "node_tree", None)
            if child_tree is not None:
                stack.append(child_tree)


def _image_looks_broken(image) -> bool:
    if image is None:
        return False
    try:
        size = tuple(getattr(image, "size", (0, 0)))
    except Exception:
        size = (0, 0)
    if len(size) < 2 or size[0] <= 0 or size[1] <= 0:
        return True
    has_data = getattr(image, "has_data", None)
    if has_data is False:
        return True
    return False


def repair_broken_dds_images_in_material(material: Material, *, allow_dds_repair: bool = False) -> bool:
    if material is None or not allow_dds_repair:
        return False

    repaired_any = False
    checked_paths = set()
    for node_tree in _iter_material_node_trees(material):
        for node in getattr(node_tree, "nodes", []):
            if getattr(node, "type", "") != 'TEX_IMAGE':
                continue
            image = getattr(node, "image", None)
            if image is None or not _image_looks_broken(image):
                continue

            image_path = win_unprefix_path(getattr(image, "filepath", "") or "")
            if not image_path or not image_path.lower().endswith(".dds"):
                continue
            path_key = win_path_key(image_path)
            if path_key in checked_paths:
                continue
            checked_paths.add(path_key)

            repaired_img, load_error = load_image_with_dds_repair(
                image_path,
                image=image,
                check_existing=True,
                allow_dds_repair=True,
            )
            if repaired_img is not None:
                node.image = repaired_img
                repaired_any = True
            else:
                log.warning("Failed to repair broken DDS image %s on material %s: %s", image_path, material.name, load_error)
    return repaired_any

def setup_w3_material(
        uncook_path: str
        ,material: Material
        ,xml_data: Element
        ,xml_path: str
        ,force_update = False	# Set to True when re-importing stuff to test changes with the latest material set-up code.
        ,is_instance_file = False
        ,defer_include_refresh = False	# Caller promises to run refresh_witcher_include_state itself (e.g. after the Base Path snapshot).
        ):
    material_started = time.perf_counter()
    resolve_seconds = 0.0
    duplicate_seconds = 0.0
    node_seconds = 0.0
    finalize_seconds = 0.0

    is_instance_file = False # This is the multi-group method, still not working

    # Checks for duplicate materials
    # Saves XML data in custom properties
    # Creates nodes
    # Loads images

    mat_base = xml_data.get('base')		# Path to the .w2mg or .w2mi file.
    if not mat_base:
        # Never seen this happen, but just in case.
        log.info("No material base, skipping: " + material.name)
        return


    params = {}
    for p in xml_data:
        params[p.get('name')] = p.get('value')

    shader_type = mat_base.split("\\")[-1][:-5]	# The .w2mg or .w2mi file, minus the extension.
    resolved_mat_base = mat_base
    inherited_params: Dict[str, tuple[str, str]] = {}
    inherited_param_sources: Dict[str, Dict[str, object]] = {}

    nodes = material.node_tree.nodes
    links = material.node_tree.links

    resolve_started = time.perf_counter()
    material_version = _material_repo_version(material)
    if mat_base.endswith(".w2mi"):
        # The XML contains little to no info about material instances, but the FBX importer
        # imported some image nodes we can use.
        fallback_shader_type = guess_shader_type(shader_type)
        w2mi_path = xml_data.get('base')
        #w2mi_tex_params = read_2wmi_params(material, uncook_path, w2mi_path, shader_type)
        inherited_params, inherited_param_sources = read_material_params_with_sources(w2mi_path, version=material_version)

        # Try to resolve the actual .w2mg base shader from the w2mi chain
        resolved_w2mg = resolve_w2mg(w2mi_path, version=material_version)
        if resolved_w2mg:
            resolved_mat_base = resolved_w2mg
            shader_type = resolved_w2mg.split("\\")[-1][:-5]
            log.info(f"Resolved shader type from w2mg: {shader_type}")
        else:
            shader_type = fallback_shader_type

    elif mat_base.endswith(".w2mg"):
        inherited_params, inherited_param_sources = read_material_params_with_sources(mat_base, version=material_version)

    if is_witcher2_material(material):
        shader_type = resolve_witcher2_shader_type(resolved_mat_base, shader_type)

    prune_unsupported_instance_params(xml_data, resolved_mat_base, params=params, version=material_version)
    resolve_seconds = time.perf_counter() - resolve_started
    shader_mapping, fallback_ng_name, resource_path = get_shader_resources_for_material(material)
    expected_ng_name = resolve_material_node_group_name(
        shader_type,
        shader_mapping,
        fallback_ng_name,
        resource_path,
    )
    if is_witcher2_material(material):
        bespoke_ng_name = resolve_w2_bespoke_group_name(resolved_mat_base, version=material_version)
        if bespoke_ng_name:
            expected_ng_name = bespoke_ng_name

    # Checking if this material was already imported by comparing some custom properties
    # that we create on imported materials.
    duplicate_started = time.perf_counter()
    material_props = getattr(material, "witcher_props", None)
    material_local = bool(getattr(material_props, "local", True))
    material_source_path = normalize_depot_path(xml_data.get("source_path", ""))
    existing_mat = find_material(
        mat_base,
        params,
        expected_ng_name=expected_ng_name,
        material_local=material_local,
        material_source_path=material_source_path,
    )
    if existing_mat:
        if overwrite_existing_enabled():
            repair_broken_dds_images_in_material(existing_mat, allow_dds_repair=True)
        if not force_update:
            duplicate_seconds = time.perf_counter() - duplicate_started
            total_seconds = time.perf_counter() - material_started
            if total_seconds >= _MATERIAL_PROFILE_WARN_THRESHOLD:
                _log_material_profile_warning(
                    "setup material %s total %.3fs (resolve %.3fs, duplicate %.3fs, node %.3fs, finalize %.3fs, reused yes)",
                    material.name,
                    total_seconds,
                    resolve_seconds,
                    duplicate_seconds,
                    node_seconds,
                    finalize_seconds,
                )
            return existing_mat
    duplicate_seconds = time.perf_counter() - duplicate_started

    # Backing up all the info from the XML into custom properties. This is used for duplicate checking.
    # (See just above)
    material['witcher3_mat_base'] = mat_base
    material['witcher3_mat_params'] = params
    material['witcher3_mat_source_path'] = material_source_path
    material['witcher3_material_setup_version'] = MATERIAL_SETUP_VERSION

    #TODO Create the material instance NodeGroup
    #TODO instances contained within w2mesh files will be imported as materials.
    if is_instance_file: #! Cange name of this option to "instance_group_mode" or something.
        node_started = time.perf_counter()
        
        all_instances = [xml_data] # xml data for each instance

        if mat_base.endswith(".w2mi"):
            final_base_mat = get_all_w2mi(mat_base, all_instances)


        #clear all nodes in the main material
        nodes.clear()
        # find each instance
        # create group for each instance
        #link them all up to the base material group at the end
        all_instances_params = []
        for i, instance_xml_data in enumerate(reversed(all_instances)):
            (ordered_params, nodegroup_node) = create_instance_group(material,
                                instance_xml_data,
                                xml_path,
                                mat_base,
                                shader_type,
                                uncook_path,
                                x_loc = -350 + i*-500)
            all_instances_params.append((ordered_params, nodegroup_node))
        
        all_instances_params_rev = all_instances_params[::-1]

        for idx in range(len(all_instances_params_rev)-1):
            from_group = all_instances_params_rev[idx]
            to_group = all_instances_params_rev[idx+1]
            for p in from_group[0]:
                par_name = p.get('name')
                try:
                    material.node_tree.links.new(from_group[1].outputs[par_name], to_group[1].inputs[par_name])
                    active_node = to_group[1].inputs.get( par_name+"_active")
                    if active_node:
                        active_node.default_value = 1.0
                    if par_name == 'DetailTile':
                        mapping_nodes = find_mapping_nodes(to_group[1].node_tree)
                        
                        def get_group_input(node_tree):
                            for node in node_tree.nodes:
                                if node.type == 'GROUP_INPUT':
                                    return node
                            return None
                        group_input = get_group_input(to_group[1].node_tree)
                        DetailTile_input = group_input.outputs['DetailTile']
                        if DetailTile_input and mapping_nodes:
                            for mapping in mapping_nodes:
                                to_group[1].node_tree.links.new(
                                    DetailTile_input,
                                    mapping.inputs[3])
                                            

                except Exception as e:
                    log.critical(f"MATERIAL ERROR {e}")
                    log.warning("Material setup error: %s", e)
        

        nodegroup_node_base_shader = init_material_nodes(material, shader_type, clear = False, base_path = resolved_mat_base)
        nodegroup_node_base_shader.name = mat_base[-60:]
        nodes_create_outputs(material, nodes, links, nodegroup_node_base_shader, xml_data, xml_path)
        for idx, p in enumerate(all_instances_params[0][0]):
            par_name = p.get('name')
            par_type = p.get('type')
            par_value = p.get('value')
            try:
                material.node_tree.links.new(all_instances_params[0][1].outputs[par_name], nodegroup_node_base_shader.inputs[par_name])
            except Exception as e:
                log.critical(f"MATERIAL ERROR {e}") #raise e
        node_seconds = time.perf_counter() - node_started
    else:
        only_basic_maps = True
        # if only_basic_maps:
        #     new_xml = ElementTree.Element(xml_data.tag, xml_data.attrib)
        #     for value in list(xml_data.iter()):
        #         if 'Diffuse' == value.attrib['name'] or 'Normal' == value.attrib['name']:
        #             new_xml.append(value)
        #     xml_data = new_xml

        node_started = time.perf_counter()
        from .nodes.domain import suspend_witcher_include_updates, refresh_witcher_include_state
        #log.warning(ElementTree.tostring(xml_data, encoding='utf8', method='xml'))
        #all_children2 = list(xml_data.iter())
        # Bulk node setup: each witcher_include write would otherwise trigger a
        # full override sync + chain re-layout, so suspend the update callback
        # and run the sync once for this material at the end.
        with suspend_witcher_include_updates():
            # Clean existing nodes and create core nodegroup.
            nodegroup_node = init_material_nodes(material, shader_type, base_path = resolved_mat_base)
            nodegroup_node.name = mat_base[-60:]

            nodes_create_outputs(material, nodes, links, nodegroup_node, xml_data, xml_path)

            # Order parameters so input nodes get created in a specified order, from top to bottom relative to the inputs of the nodegroup.
            # Purely for neatness of the node noodles.
            ordered_params = order_elements_by_attribute(xml_data, PARAM_ORDER, 'name')

            mark_imported_params_as_local(
                ordered_params,
                params,
                material_local=material_local,
            )

            #links nodes to created output
            #! Missing params will be created by this function
            mat_load_params_into_nodes(material, ordered_params, nodegroup_node, uncook_path)
            apply_shader_default_overrides(material, nodegroup_node, inherited_params, uncook_path, inherited_param_sources)
            if shader_type == 'pbr_eye':
                setup_eye_reflection_nodes(material, nodegroup_node, nodes, links)
            hide_unused_sockets(nodegroup_node)

            if existing_mat and force_update:
                existing_mat.user_remap(material)

            #if the material is a .w2mi file use the filename, otherwise use diffues name for materal
            if not is_instance_file:
                pass
                #mat_set_name_by_diffuse(material, nodegroup_node, nodes)
            mat_ensure_dummy_transparent_img_node(material, nodegroup_node, shader_type, nodes)
            mat_apply_settings(material, shader_type)
        if not defer_include_refresh:
            refresh_witcher_include_state(material)
        node_seconds = time.perf_counter() - node_started
    finalize_seconds = max(0.0, time.perf_counter() - material_started - resolve_seconds - duplicate_seconds - node_seconds)
    total_seconds = time.perf_counter() - material_started
    if total_seconds >= _MATERIAL_PROFILE_WARN_THRESHOLD:
        _log_material_profile_warning(
            "setup material %s total %.3fs (resolve %.3fs, duplicate %.3fs, node %.3fs, finalize %.3fs, reused no)",
            material.name,
            total_seconds,
            resolve_seconds,
            duplicate_seconds,
            node_seconds,
            finalize_seconds,
        )
    return material

def find_material(
        mat_base,
        params,
        expected_ng_name: str = "",
        *,
        material_local=None,
        material_source_path=None,
        ):
    """Find a material based on the Witcher 3 shader type and shader parameters,
    which we store in custom properties on import.
    This is useful for checking whether a material was already imported.
    """
    for m in bpy.data.materials:
        if (
            m.get('witcher3_material_setup_version', 0) == MATERIAL_SETUP_VERSION and \
            'witcher3_mat_params' in m and \
            mat_base == m['witcher3_mat_base'] and \
            params == m['witcher3_mat_params'].to_dict()
        ):
            if material_local is not None and not material_local_mode_matches(m, material_local):
                continue
            if material_source_path is not None:
                cached_source_path = normalize_depot_path(m.get('witcher3_mat_source_path', ''))
                if cached_source_path != normalize_depot_path(material_source_path):
                    continue
            if expected_ng_name:
                active_group = get_active_witcher_group_node(m)
                active_tree = getattr(active_group, "node_tree", None) if active_group else None
                active_ng_name = str(getattr(active_tree, "name", "") or "")
                if not _node_group_names_match(active_ng_name, expected_ng_name):
                    continue
            # A material with the same parameters is already imported,
            return m

def read_2wmi_params2(
        material_bin: str
        ) -> Dict[str, tuple[str, str]]:
    return _read_material_params_from_bin(material_bin)

def read_2wmi_params(
        w2mi_path: str,
        version: int = 999,
        ) -> Dict[str, tuple[str, str]]:
    # Check if the .w2mi file references any textures or texarrays, and do the same there.
    # Load the .w2mi file.
    log.info("READING W2MI: " + w2mi_path) # FIX PATHS WITH SPACES bob_broken_woods_longpile

    return read_material_params_from_path(w2mi_path, version=version)


def read_material_params_with_sources(
        material_path: str,
        version: int = 999,
        ) -> Tuple[Dict[str, tuple[str, str]], Dict[str, Dict[str, object]]]:
    chain_info = collect_material_chain(material_path, version=version)
    chain = chain_info.get("chain", []) or []
    params: Dict[str, tuple[str, str]] = {}
    sources: Dict[str, Dict[str, object]] = {}
    source_index_by_entry = {id(entry): idx for idx, entry in enumerate(chain)}

    def remember(entry, par_name: str, attrs, source_kind: str) -> None:
        params[par_name] = attrs
        sources[par_name] = {
            "source_kind": source_kind,
            "source_path": entry.get("path", ""),
            "source_index": source_index_by_entry.get(id(entry), -1),
            "chunk_type": entry.get("chunk_type", ""),
        }

    graph_entry = next((entry for entry in chain if entry.get("chunk_type") == "CMaterialGraph"), None)
    if graph_entry is not None:
        for par_name, attrs in read_local_material_params_from_bin(graph_entry.get("_material_bin")).items():
            remember(graph_entry, par_name, attrs, "graph_default")

    for entry in reversed([entry for entry in chain if entry.get("chunk_type") == "CMaterialInstance"]):
        for par_name, attrs in read_local_material_params_from_bin(entry.get("_material_bin")).items():
            remember(entry, par_name, attrs, "instance")

    if not params:
        params = read_material_params_from_path(material_path, version=version)
    return params, sources


def guess_texture_type_by_link(mat: Material, img_node):
        socket_name = img_node.outputs[0].links[0].to_socket.name
        if socket_name == 'Base Color':
            return 'Diffuse'
        if socket_name == 'Color':	# Normal maps are connected to a Normal Map node's "Color" input.
            return 'Normal'
        else:
            log.info(f"Image {img_node.image.name} on material {mat.name} attaches to {socket_name}, yo!")
            return

def create_param(
            xml_data: Element
            ,name: str
            ,type: str
            ,value: str
            ,**extra_attrs
        ) -> Element:
    """Create a parameter sub-Element in the xml_data Element."""
    new_param = ElementTree.SubElement(xml_data, 'param')
    new_param.set('name', _sanitize_xml_attr(name, "param"))
    new_param.set('type', _sanitize_xml_attr(type, "Float"))
    new_param.set('value', _sanitize_xml_attr(value))
    for attr_name, attr_value in extra_attrs.items():
        if attr_value is not None:
            new_param.set(attr_name, str(attr_value))

    return new_param

def create_texture_param(
            xml_data: Element
            ,name: str
            ,tex_filepath: str
        ) -> Element:
    """Create a texture parameter sub-Element in the xml_data Element."""
    new_param = ElementTree.SubElement(xml_data, 'param')
    new_param.set('name', _sanitize_xml_attr(name, "Texture"))
    new_param.set('type', 'handle:ITexture')

    # The param's 'value' needs to be the texture path relative to the uncook folder.
    new_param.set('value', _sanitize_xml_attr(tex_filepath))

    return new_param

def is_file_referenced_in_xml(xml_data: ElementTree, search_file: str) -> bool:
    """Return whether any sub-Elements of an Element reference a given filename.
    The path to the file is ignored, only the filename (including extension) is compared.
    """
    for param in xml_data:
        par_type = param.get('type')
        par_value = param.get('value')
        if par_type != 'handle:ITexture' or par_value == 'NULL':
            continue

        filename = par_value.split("\\")[-1]
        if filename == search_file:
            # This parameter references a file with this name!
            return True

    # No parameters referenced the searched file.
    return False

def guess_shader_type(shader_type: str) -> str:
    """Guesssing the shader type. This is to simplify the set of shaders found in the game.
    Eg., the game has several hair and skin shaders, but we have no way to know the
    difference between these, so we just use a smaller number of shaders.
    """
    if 'sword_rune' in shader_type:
        return 'sword_final'
    if 'hair' in shader_type:
        return 'pbr_hair'
    if 'skin' in shader_type:
        return 'pbr_skin'
    if 'eye' in shader_type and "eyelashes" not in shader_type:
        return 'pbr_eye'
    if 'transparent_lit' in shader_type:
        return 'transparent_lit'
    if 'component__shadow' in shader_type:
        return 'pbr_eye_shadow'

    return 'pbr_std'

def init_material_nodes(material: Material, shader_type: str, clear:bool = True, base_path: str = ""):
    """Wipe all nodes, then create a node group node and return it."""
    shader_mapping, fallback_ng_name, resource_path = get_shader_resources_for_material(material)
    ng_name = resolve_material_node_group_name(shader_type, shader_mapping, fallback_ng_name, resource_path)
    if ng_name == fallback_ng_name and shader_type not in shader_mapping and shader_type != fallback_ng_name:
        log.debug(f"Unknown shader type: {shader_type} (Fell back to default)")
    ng = ensure_node_group(ng_name, resource_path=resource_path)			# Nodegroup node tree  (bpy.types.ShaderNodeTree)
    node_ng = None							# Nodegroup group node (bpy.types.ShaderNodeGroup)
    assert ng, f"Node group {ng_name} not found. Resources didn't append correctly?"

    if base_path and is_witcher2_material(material):
        # W2 shader graphs get a bespoke copy of the base group with pins
        # renamed to the parameters the .w2mg actually declares.
        bespoke_ng = get_or_create_w2_bespoke_node_group(ng, base_path, version=_material_repo_version(material))
        if bespoke_ng is not None:
            ng = bespoke_ng

    nodes = material.node_tree.nodes
    if clear:
        # Wipe nodes created by fbx importer.
        nodes.clear()

    # Create main node group node
    node_ng = nodes.new(type='ShaderNodeGroup')
    node_ng.node_tree = ng
    node_ng.label = shader_type

    node_ng.location = (500, 200)
    node_ng.width = 350

    return node_ng

def init_instance_nodes(material: Material, shader_type: str, clear:bool = True, x_loc:int = -250):
    """Wipe all nodes, then create a node group node and return it."""
    ng_name = material.name #SHADER_MAPPING.get(shader_type)
    ng = bpy.data.node_groups.new(ng_name, 'ShaderNodeTree')
    nodes = material.node_tree.nodes
    
    if clear:
        nodes.clear()

    # Create main node group node
    node_ng = nodes.new(type='ShaderNodeGroup')
    node_ng.node_tree = ng
    node_ng.label = ng_name

    node_ng.location = (x_loc, 200)
    node_ng.width = 350

    return node_ng

def nodes_create_outputs(material, nodes, links, node_ng, xml_data, xml_path):
    """Create and link up separate output nodes for Cycles and Eevee."""
    node_output_default = nodes.new(type='ShaderNodeOutputMaterial')
    node_output_default.location = (900, 200)
    node_output_default.name = xml_path[-60:]
    links.new(node_ng.outputs[0], node_output_default.inputs[0])

    if len(node_ng.outputs) == 1:
        return node_output_default

    node_output_default.target = 'CYCLES'

    node_output_eevee = nodes.new(type='ShaderNodeOutputMaterial')
    node_output_eevee.target = 'EEVEE'
    node_output_eevee.location = (900, 0)
    node_output_eevee.name = xml_path[-60:]
    links.new(node_ng.outputs[1], node_output_eevee.inputs[0])

def _find_socket_by_name(sockets, name: str):
    if not name:
        return None
    try:
        socket = sockets.get(name)
        if socket is not None:
            return socket
    except Exception:
        pass
    for socket in sockets:
        if getattr(socket, "name", None) == name:
            return socket
    return None


def find_group_input_socket(node_ng: Node, par_name: str):
    return _find_socket_by_name(node_ng.inputs, par_name)


def _ensure_witcher3_skin_subsurface(node_tree) -> None:
    group_input = next((node for node in node_tree.nodes if node.type == 'GROUP_INPUT'), None)
    principled = next((node for node in node_tree.nodes if node.type == 'BSDF_PRINCIPLED'), None)
    if group_input is None or principled is None:
        return

    def socket(sockets, *names):
        for name in names:
            found = _find_socket_by_name(sockets, name)
            if found is not None:
                return found
        return None

    source = socket(group_input.outputs, 'SubsurfaceScale', 'Subsurface Scale')
    weight = socket(principled.inputs, 'Subsurface Weight', 'Subsurface')
    if source is None or weight is None or weight.is_linked:
        return

    scale = node_tree.nodes.get('W3 Skin Subsurface Weight')
    if scale is not None and scale.type != 'MATH':
        return
    if scale is None:
        scale = node_tree.nodes.new('ShaderNodeMath')
        scale.name = 'W3 Skin Subsurface Weight'
    scale.label = 'Skin Scale to Weight'
    scale.operation = 'MULTIPLY'
    scale.use_clamp = True
    scale.inputs[1].default_value = 0.4
    scale.location = (principled.location.x - 220, principled.location.y - 140)
    if not scale.inputs[0].is_linked:
        node_tree.links.new(source, scale.inputs[0])
    node_tree.links.new(scale.outputs[0], weight)

    if hasattr(principled, 'subsurface_method'):
        for method in ('RANDOM_WALK_SKIN', 'RANDOM_WALK'):
            try:
                principled.subsurface_method = method
                break
            except (TypeError, ValueError):
                pass

    radius = socket(principled.inputs, 'Subsurface Radius')
    scale = socket(principled.inputs, 'Subsurface Scale')
    if radius is not None and not radius.is_linked:
        radius.default_value = (1.0, 0.35, 0.2) if scale is not None else (0.01, 0.0035, 0.002)
    if scale is not None and not scale.is_linked:
        scale.default_value = 0.01


def _ensure_witcher3_eye_shader(node_tree) -> None:
    group_input = next((node for node in node_tree.nodes if node.type == 'GROUP_INPUT'), None)
    group_output = next((node for node in node_tree.nodes if node.type == 'GROUP_OUTPUT'), None)
    principled = next((node for node in node_tree.nodes if node.type == 'BSDF_PRINCIPLED'), None)
    if group_input is None or group_output is None or principled is None:
        return

    emission = node_tree.nodes.get('W3 Eye Blick Emission') or next(
        (node for node in node_tree.nodes if node.type == 'EMISSION'),
        None,
    )
    if emission is None:
        emission = node_tree.nodes.new('ShaderNodeEmission')
    emission.name = 'W3 Eye Blick Emission'
    emission.label = 'REDengine Blick'
    emission.inputs['Strength'].default_value = 1.0

    add_shader = node_tree.nodes.get('W3 Eye Additive Blick')
    if add_shader is None or add_shader.type != 'ADD_SHADER':
        if add_shader is not None:
            node_tree.nodes.remove(add_shader)
        add_shader = node_tree.nodes.new('ShaderNodeAddShader')
        add_shader.name = 'W3 Eye Additive Blick'
    add_shader.label = 'Base + Blick'

    def replace_input(source, target) -> None:
        while target.is_linked:
            node_tree.links.remove(target.links[0])
        node_tree.links.new(source, target)

    blick = _find_socket_by_name(group_input.outputs, 'BlickCube')
    specular = _find_socket_by_name(group_input.outputs, 'Specular')
    specular_input = _find_socket_by_name(principled.inputs, 'Specular IOR Level')
    if specular_input is None:
        specular_input = _find_socket_by_name(principled.inputs, 'Specular')
    if blick is not None:
        replace_input(blick, emission.inputs['Color'])
    if specular is not None and specular_input is not None:
        replace_input(specular, specular_input)

    replace_input(principled.outputs[0], add_shader.inputs[0])
    replace_input(emission.outputs[0], add_shader.inputs[1])
    for output_name in ('Cycles', 'Eevee'):
        output = _find_socket_by_name(group_output.inputs, output_name)
        if output is not None:
            replace_input(add_shader.outputs[0], output)


def get_active_witcher_group_node(material: Optional[Material]) -> Optional[Node]:
    if material is None or getattr(material, "node_tree", None) is None:
        return None
    nodes = material.node_tree.nodes
    active_outputs = [
        node for node in nodes
        if node.type == 'OUTPUT_MATERIAL' and bool(getattr(node, "is_active_output", True))
    ]
    if not active_outputs:
        active_outputs = [node for node in nodes if node.type == 'OUTPUT_MATERIAL']
    if not active_outputs:
        return None

    for node in nodes:
        if node.type != 'GROUP' or getattr(node, "node_tree", None) is None:
            continue
        for output_socket in getattr(node, "outputs", []):
            for link in getattr(output_socket, "links", []):
                if link.to_node in active_outputs:
                    return node
    return None


def _values_differ(expected, actual, tolerance: float = 1e-5) -> bool:
    try:
        if isinstance(expected, (tuple, list)) and isinstance(actual, (tuple, list)):
            if len(expected) != len(actual):
                return True
            return any(abs(float(a) - float(b)) > tolerance for a, b in zip(expected, actual))
        return abs(float(expected) - float(actual)) > tolerance
    except Exception:
        return expected != actual


def _coerce_scalar_socket_default(value):
    try:
        return float(value)
    except Exception:
        pass

    if isinstance(value, (str, bytes)):
        return None
    try:
        values = tuple(value)
    except Exception:
        return None
    if len(values) != 1:
        return None
    try:
        return float(values[0])
    except Exception:
        return None


def _shader_default_differs(node_ng: Node, input_pin, par_type: str, par_value: str) -> bool:
    if input_pin is None:
        return False

    if par_type == 'Float':
        current_default = _coerce_scalar_socket_default(input_pin.default_value)
        if current_default is None:
            log.debug(
                "Ignoring shader default Float override for %s: socket default is not scalar",
                getattr(input_pin, "name", ""),
            )
            return False
        return _values_differ(float(par_value), current_default)

    if par_type == 'Color':
        values = [float(f.strip()) for f in par_value.split(";")]
        if not hasattr(input_pin.default_value, "__iter__"):
            return False
        normalized = tuple(value / 255.0 for value in values[:4])
        current = tuple(input_pin.default_value[:4])
        return _values_differ(normalized, current)

    if par_type == 'Vector':
        values = [float(f.strip()) for f in par_value.split(";")]
        if not hasattr(input_pin.default_value, "__iter__"):
            return False
        current_xyz = tuple(input_pin.default_value[:3])
        if _values_differ(tuple(values[:3]), current_xyz):
            return True
        if len(values) > 3:
            existing_w = None
            if len(input_pin.links) != 0:
                try:
                    linked_node = input_pin.links[0].from_socket.node
                    if str(getattr(linked_node, "witcher_param_kind", "") or "") == VECTOR_PARAM_KIND:
                        existing_w = get_vector_w(linked_node, None)
                except Exception:
                    existing_w = None
            if existing_w is None:
                existing_w = get_legacy_w_value(input_pin, None)
            if existing_w is not None:
                return _values_differ(values[3], float(existing_w))
        return False

    if par_type in ('handle:ITexture', 'handle:CTextureArray', 'handle:CCubeTexture'):
        return True

    return False


def build_param_element(name: str, param_type: str, value: str, **extra_attrs) -> Element:
    param = ElementTree.Element('param')
    param.set('name', _sanitize_xml_attr(name, "param"))
    param.set('type', _sanitize_xml_attr(param_type, "Float"))
    param.set('value', _sanitize_xml_attr(value))
    for attr_name, attr_value in extra_attrs.items():
        if attr_value is not None:
            param.set(attr_name, str(attr_value))
    return param


def _tag_created_material_source_nodes(
        mat: Material,
        existing_node_ptrs: Set[int],
        param: Element,
        param_name: str,
        param_type: str,
        ) -> None:
    source_path = str(param.get("witcher_source_path") or "")
    source_kind = str(param.get("witcher_source_kind") or "")
    source_index = coerce_source_index(param.get("witcher_source_index"))
    source_row_index = coerce_source_index(param.get("witcher_source_row_index"))
    source_row_y = coerce_source_index(param.get("witcher_source_row_y"))
    source_color = chain_color_for_index(source_index)
    if not source_path and source_index < 0:
        return

    for created_node in getattr(getattr(mat, "node_tree", None), "nodes", []) or []:
        try:
            if created_node.as_pointer() in existing_node_ptrs:
                continue
            created_node["witcher_material_source_path"] = source_path
            created_node["witcher_material_source_kind"] = source_kind
            created_node["witcher_material_source_param"] = param_name or ""
            created_node["witcher_material_source_type"] = param_type or ""
            created_node["witcher_material_source_index"] = int(source_index)
            created_node["witcher_material_source_row_index"] = int(source_row_index)
            created_node["witcher_material_source_row_y"] = int(source_row_y)
            if source_path:
                created_node["witcher_base_material_source"] = source_path
            if source_kind:
                created_node["witcher_base_material_source_kind"] = source_kind
            if source_color is not None:
                created_node.use_custom_color = True
                created_node.color = source_color
                created_node["witcher_material_chain_source_index"] = int(source_index)
        except Exception:
            continue


def build_shader_default_override_params(
        node_ng: Node,
        inherited_params: Dict[str, tuple[str, str]],
        inherited_param_sources: Optional[Dict[str, Dict[str, object]]] = None,
        ) -> List[Element]:
    if not inherited_params:
        return []

    shader_default_params: List[Element] = []
    for par_name, attrs in inherited_params.items():
        par_type, par_value = attrs
        input_pin = find_group_input_socket(node_ng, par_name)
        if input_pin is None:
            continue
        if len(input_pin.links) != 0:
            continue
        if not _shader_default_differs(node_ng, input_pin, par_type, par_value):
            continue

        source_info = (inherited_param_sources or {}).get(par_name, {})
        shader_default_params.append(
            build_param_element(
                par_name,
                par_type,
                par_value,
                witcher_shader_default="true",
                witcher_require_socket="true",
                witcher_source_name=par_name,
                witcher_source_path=source_info.get("source_path", ""),
                witcher_source_kind=source_info.get("source_kind", ""),
                witcher_source_index=source_info.get("source_index", -1),
                witcher_source_row_index=source_info.get("row_index", -1),
                witcher_source_row_y=source_info.get("row_y", ""),
            )
        )

    return shader_default_params


def mark_shader_default_node(node):
    if not node:
        return
    node.use_custom_color = True
    node.color = (0.38, 0.52, 0.22)
    try:
        node["witcher_shader_default"] = True
    except Exception:
        pass


def _next_shader_default_y(mat: Material) -> int:
    nodes = mat.node_tree.nodes
    if not nodes:
        return 1000
    min_y = min(int(getattr(node.location, "y", node.location[1])) for node in nodes)
    return min_y - 170


def apply_shader_default_overrides(
        mat: Material,
        node_ng: Node,
        inherited_params: Dict[str, tuple[str, str]],
        uncook_path: str,
        inherited_param_sources: Optional[Dict[str, Dict[str, object]]] = None,
        ):
    shader_default_params = build_shader_default_override_params(node_ng, inherited_params, inherited_param_sources)
    if not shader_default_params:
        return

    ordered_defaults = order_elements_by_attribute(shader_default_params, PARAM_ORDER, 'name')
    y_loc = _next_shader_default_y(mat)
    for param in ordered_defaults:
        node = create_node_for_param(mat, param, node_ng, uncook_path, y_loc)
        if not node:
            continue
        if node.type == 'TEX_IMAGE':
            y_loc -= 320
        elif node.type == 'RGB':
            y_loc -= 220
        else:
            y_loc -= 170

def order_elements_by_attribute(
        elements: List[Element]
        ,order: List[str]
        ,attribute = 'name'
    ) -> List[Element]:
    """Return a list of Element objects ordered by the value of an
    attribute and an arbitrary order. Used to order nodes so that more
    useful input nodes are at the top of the node graph, and
    miscellanaea are at the bottom.
    """
    ordered = []
    unordered = elements[:]
    for name in order:
        for p in elements:
            if p.get('name') == name:
                ordered.append(p)
                if p in unordered:
                    unordered.remove(p)
    ordered.extend(unordered)
    return ordered

def mat_load_params_into_nodes(
        mat: Material
        ,ordered_params: List[Element]
        ,node_ng: Node
        ,uncook_path: str
    ):
    """Load parameters into nodes."""

    texarray_index = '0'
    for param1 in ordered_params:
        if param1.attrib['name'] == "Pattern_Index":
            texarray_index = param1.attrib['value']

    y_loc = 1000	# Y location of the next param node to spawn.
    for param in ordered_params:
        node = create_node_for_param(mat, param, node_ng, uncook_path, y_loc, texarray_index)
        if not node:
            continue
        if node.type == 'TEX_IMAGE':
            y_loc -= 320
        elif node.type == 'RGB':
            y_loc -= 220
        else:
            y_loc -= 170
        if param.get("witcher_include"):
            node.witcher_include = True
            try:
                node.witcher_export = True
            except Exception:
                pass


def _is_uv_mapping_vector_param(param_name: str) -> bool:
    if not param_name:
        return False
    return (
        'Tile' in param_name
        or 'Rotation' in param_name
        or 'Offset' in param_name
        or param_name == 'SpecularShiftUVScale'
    )


def _find_texture_mapping_node(nodes, mat: Material, vector_param_name: str, target_node_name: str):
    target_node = nodes.get(target_node_name)
    if not target_node:
        return None
    if len(target_node.inputs[0].links) == 0:
        log.warning(
            "Warning: Node %s in material %s was expected to have a Mapping node plugged into it!",
            target_node.name,
            mat.name,
        )
        return None
    mapping_node = target_node.inputs[0].links[0].from_node
    if mapping_node.type != 'MAPPING':
        log.warning("Expected a mapping node for %s, got %s instead!", vector_param_name, mapping_node.type)
        return None
    return mapping_node


def _get_uv_mapping_targets(mat: Material, param_name: str):
    nodes = mat.node_tree.nodes
    mapping_targets = []
    replace_token = None

    if 'Rotation' in param_name:
        replace_token = 'Rotation'
    elif 'Offset' in param_name:
        replace_token = 'Offset'
    elif 'Tile' in param_name:
        replace_token = 'Tile'

    if replace_token == 'Tile':
        for name in ['Diffuse', 'Normal']:
            target_name = param_name.replace(replace_token, name)
            mapping_node = _find_texture_mapping_node(nodes, mat, param_name, target_name)
            if mapping_node and mapping_node not in mapping_targets:
                mapping_targets.append(mapping_node)
        if param_name == 'DetailTile':
            pattern_mapping = nodes.get(_internal_helper_node_name("Mapping", "Pattern_Array"))
            if pattern_mapping and pattern_mapping.type == 'MAPPING' and pattern_mapping not in mapping_targets:
                mapping_targets.append(pattern_mapping)
    elif replace_token in {'Rotation', 'Offset'}:
        for name in ['Diffuse', 'Normal']:
            target_name = param_name.replace(replace_token, name)
            mapping_node = _find_texture_mapping_node(nodes, mat, param_name, target_name)
            if mapping_node and mapping_node not in mapping_targets:
                mapping_targets.append(mapping_node)
    elif param_name == 'SpecularShiftUVScale':
        mapping_node = _find_texture_mapping_node(nodes, mat, param_name, 'SpecularShiftTexture')
        if mapping_node:
            mapping_targets.append(mapping_node)

    return mapping_targets


def _apply_uv_mapping_vector_links(mat: Material, param_name: str, vector_node: Optional[Node]) -> None:
    if mat is None or vector_node is None or not _is_uv_mapping_vector_param(param_name):
        return

    links = mat.node_tree.links
    values = get_vector_node_values(vector_node, param_name)
    for mapping_node in _get_uv_mapping_targets(mat, param_name):
        mapping_input = get_mapping_vector_input(mapping_node, param_name)
        if mapping_input is None:
            continue
        mapping_node.label = f"{param_name} Mapping"
        for idx in range(min(3, len(mapping_input.default_value))):
            mapping_input.default_value[idx] = values[idx]
        if mapping_input.is_linked and mapping_input.links[0].from_socket.node == vector_node:
            continue
        if not mapping_input.is_linked:
            links.new(vector_node.outputs[0], mapping_input)


def reconcile_uv_mapping_vector_links(mat: Material) -> None:
    if mat is None or mat.node_tree is None:
        return

    for node in mat.node_tree.nodes:
        if node.type != 'COMBXYZ':
            continue
        param_name = str(getattr(node, "witcher_param_name", "") or node.name or "")
        if not _is_uv_mapping_vector_param(param_name):
            continue
        _apply_uv_mapping_vector_links(mat, param_name, node)


_SRGB_TEXTURE_PIN_NAMES = {'Diffuse', 'SpecularTexture', 'SnowDiffuse'}


def _is_srgb_texture_param(pin_name: str, par_name: str, is_w2_material: bool = False) -> bool:
    if pin_name in _SRGB_TEXTURE_PIN_NAMES or 'DiffuseArray' in (par_name or ""):
        return True
    # W2 graphs use their own parameter names (diffusemap, specular, ...).
    return is_w2_material and is_w2_srgb_texture_param(pin_name)


def fix_texture_node(par_name, node, is_w2_material: bool = False):
    if node and node.image:
        if _is_srgb_texture_param(par_name, par_name, is_w2_material):
            node.image.colorspace_settings.name = 'sRGB'
        else:
            node.image.colorspace_settings.name = 'Non-Color'

    if node and node.image and len(node.outputs[0].links) > 0:
        pin_name = node.outputs[0].links[0].to_socket.name
        if _is_srgb_texture_param(pin_name, par_name, is_w2_material):
            node.image.colorspace_settings.name = 'sRGB'
        else:
            node.image.colorspace_settings.name = 'Non-Color'
    return node

def node_tree_inputs_new(node_ng, par_type, par_name ):
    if bpy.app.version >= (4, 0, 0):
        node_ng.node_tree.interface.new_socket(name=par_name, in_out='INPUT', socket_type=par_type)
    else:
        node_ng.node_tree.inputs.new(par_type, par_name)


def _resolve_texarray_source_path(texarray_value: str, uncook_path: str, repo_version: int) -> str:
    texarray_value = str(texarray_value or "")
    if not texarray_value:
        return ""

    if os.path.isabs(texarray_value):
        return texarray_value

    try:
        resolved_path = repo_file(texarray_value, version=repo_version)
        if resolved_path and os.path.exists(win_safe_path(resolved_path)):
            return resolved_path
    except Exception:
        log.debug("Failed to resolve texarray through repo_file: %s", texarray_value, exc_info=True)

    return os.path.join(uncook_path, texarray_value)


def _legacy_texarray_slice_paths(texarray_value: str, uncook_path: str) -> list[str]:
    tex_index = 0
    paths = []
    tex_ext = get_tex_ext(bpy.context)
    texarray_path = os.path.abspath(uncook_path + os.sep + f"{texarray_value}.texture_{tex_index}{tex_ext}")
    create_one = True

    while create_one or Path(texarray_path).exists():
        create_one = False
        paths.append(texarray_path)
        tex_index += 1
        texarray_path = os.path.abspath(uncook_path + os.sep + f"{texarray_value}.texture_{tex_index}{tex_ext}")

    return paths


def _coerce_texarray_index(texarray_index) -> int:
    try:
        return max(0, int(float(texarray_index)))
    except Exception:
        return 0


def create_node_for_param(
        mat: Material
        ,param: Element
        ,node_ng: Node
        ,uncook_path: str
        ,y_loc: int
        ,texarray_index: int = 0
    ) -> bpy.types.Node:
    """Create and hook up the nodes for a Witcher 3 shader parameter to the primary nodegroup."""
    links = mat.node_tree.links
    existing_node_ptrs = {
        node.as_pointer()
        for node in getattr(mat.node_tree, "nodes", []) or []
    }

    par_name = param.get('name')
    par_type = param.get('type')
    par_value = param.get('value')

    if 'debug' in par_value:
        return

    if par_value == 'NULL': #or par_name in IGNORED_PARAMS:
        return
    if par_name in IGNORED_PARAMS:
        log.debug('Skipping ignored param %s', par_name)
        return

    node_label = par_name
    node = None
    require_existing_socket = param.get("witcher_require_socket") == "true"
    is_shader_default = param.get("witcher_shader_default") == "true"
    input_pin = find_group_input_socket(node_ng, par_name)

    if require_existing_socket and input_pin is None:
        log.debug("Skipping shader default param %s: no exact matching socket on nodegroup", par_name)
        return

    if par_type in ['handle:ITexture']:
        node = create_node_texture(mat, param, node_ng, y_loc, uncook_path, texarray_index)
        node = fix_texture_node(par_name, node, is_w2_material=is_witcher2_material(mat))

    elif par_type == 'handle:CCubeTexture':
        node = create_node_cubemap(mat, param, node_ng, y_loc, uncook_path)

    elif par_type in ['handle:CTextureArray']:
        #create all the textures for this array
        #create the tex array and link it

        texture_array = []
        texture_paths = []

        texarray_source_path = _resolve_texarray_source_path(par_value, uncook_path, _material_repo_version(mat))
        if texarray_source_path and os.path.exists(win_safe_path(texarray_source_path)):
            try:
                texture_paths = convert_texarray_to_dds(texarray_source_path)
            except Exception:
                log.warning("Failed to convert texarray '%s'", texarray_source_path, exc_info=True)

        if not texture_paths:
            texture_paths = _legacy_texarray_slice_paths(par_value, uncook_path)

        for tex_index, texarray_path in enumerate(texture_paths):
            sub_param = {
                'name' : os.path.basename(texarray_path),
                'value' :texarray_path
            }
            sub_node = create_node_texture(mat, sub_param, node_ng, y_loc, uncook_path, texarray_index)
            sub_node = fix_texture_node(par_name, sub_node, is_w2_material=is_witcher2_material(mat))
            sub_node.location = (-800, y_loc)
            texture_array.append(sub_node)

        #full_path = os.path.join(get_uncook_path(bpy.context), par_value)
        #texarray_ = CR2W_reader.load_material(full_path)

        tex_array_group = create_texarray( ARRAY_SIZE = len(texture_array))
        TexArray_ng = mat.node_tree.nodes.new(type='ShaderNodeGroup')
        TexArray_ng.node_tree = tex_array_group
        TexArray_ng.location = (200, 0)
        texarray_repo_path = normalize_depot_path(par_value)
        TexArray_ng["witcher_texarray_source_path"] = texarray_repo_path
        TexArray_ng["witcher_material_param_type"] = "handle:CTextureArray"
        try:
            TexArray_ng.witcher_texarray_source_path = texarray_repo_path
        except Exception:
            pass
        
        for idx, sub_n in enumerate(texture_array):
            links.new(sub_n.outputs[0], TexArray_ng.inputs[idx])
        
        
        #node_ng.width = 350
        node = TexArray_ng

    elif par_type == 'Float':
        node = create_node_float(mat, param, node_ng)
    elif par_type == 'Color':
        node = create_node_color(mat, param, node_ng)
    elif par_type == 'Vector':
        node = create_node_vector(mat, param, node_ng, do_vec_4 = True)
    else:
        log.debug("Unhandled material parameter type: %s", par_type)
        node = create_node_attribute(mat, param, node_ng)
        node_label = "Unknown type: " + par_type

    if not node:
        return

    node.location = (-450, y_loc)
    node.name = par_name
    node.label = node_label

    #this will create the input pin on the shader node gorup if it doesn't exist. Idealy all shader pins would be defined. But some w2mi have values that don't exist on their shader
    #TODO check for same names but differnt types defined on instance vs shader.
    if input_pin == None:
        if require_existing_socket:
            return
        if par_type == "Color":
            node_tree_inputs_new( node_ng, 'NodeSocketColor', par_name)
        elif par_type == "Float":
            node_tree_inputs_new( node_ng, 'NodeSocketFloat', par_name)
        elif par_type == "handle:ITexture":
            node_tree_inputs_new( node_ng, 'NodeSocketColor', par_name)
        elif par_type == 'handle:CTextureArray':
            node_tree_inputs_new( node_ng, 'NodeSocketColor', par_name)
        elif par_type == 'Vector':
            node_tree_inputs_new( node_ng, 'NodeSocketVector', par_name)
        input_pin = _find_socket_by_name(node_ng.inputs, par_name)

    if is_shader_default:
        mark_shader_default_node(node)

    if input_pin and len(input_pin.links) == 0:
        # Only connect the node if some other node isn't already connected.
        # This is because if there are two diffuse textures defined, we are better off prioritizing
        # the first one.
        try:
            links.new(node.outputs[0], input_pin)
            # Connect texture alpha to {par_name}_alpha if that input exists on the nodegroup
            if par_type in ['handle:ITexture'] and node.type == 'TEX_IMAGE':
                alpha_pin = node_ng.inputs.get(f"{par_name}_alpha")
                if alpha_pin and len(alpha_pin.links) == 0:
                    links.new(node.outputs[1], alpha_pin)
        except Exception as e:
            log.critical(f'PIN LINKING ERROR {e}')
    try:
        if input_pin and input_pin.is_linked and input_pin.links[0].from_socket.node == node:
            reconcile_uv_mapping_vector_links(mat)
    except Exception:
        pass
    _tag_created_material_source_nodes(mat, existing_node_ptrs, param, par_name, par_type)
    return node


def create_node_texture(
        mat: Material
        ,param: Element
        ,node_ng: Node
        ,y_loc: int
        ,uncook_path: str
        ,texarray_index: str = '0'
        ,using_node_tree:bool = False
    ):
    texture_started = time.perf_counter()
    conversion_seconds = 0.0
    image_load_seconds = 0.0
    converted_from_xbm = False
    converted_from_dds = False
    if using_node_tree:
        nodes = node_ng.nodes
        links = node_ng.links
    else:
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links

    par_name = param.get('name')
    par_value = param.get('value')
    repo_version = _material_repo_version(mat)
    texarray_source_repo_path = ""

    node = nodes.new(type="ShaderNodeTexImage")
    node.width = 300

    # Some texture types need special treatment.
    if par_name == 'Normal':
        # Roughness is stored in the alpha channel of Normal maps, so let's connect it.
        roughness_pin = node_ng.inputs.get('Roughness')
        if roughness_pin:
            links.new(node.outputs[1], roughness_pin)
    elif par_name == 'Diffuse':
        # Similarly, the alpha channel of the diffuse is of course used for transparency.
        alpha_pin = node_ng.inputs.get('Alpha')
        if alpha_pin and len(alpha_pin.links) == 0:
            links.new(node.outputs[1], alpha_pin)
    elif par_name in ['SpecularShiftTexture', 'SnowDiffuse', 'SnowNormal', 'Pattern_Array'] or \
            ('Normal' in par_name and 'Detail' in par_name):
        # DetailNormals need a Mapping node to apply the DetailScale and DetailRotation to.
        # Snow textures also need a Mapping node to apply the SnowTile value to.
        node_mapping = nodes.new(type='ShaderNodeMapping')
        node_mapping.location = (-600, y_loc-200)
        node_mapping.hide = True
        node_mapping.label = f"{par_name} Mapping"
        node_mapping.name = _internal_helper_node_name("Mapping", par_name)
        links.new(node_mapping.outputs[0], node.inputs[0])

        node_uv = nodes.new(type='ShaderNodeUVMap')
        node_uv.location = (node_mapping.location.x-200, node_mapping.location.y)
        node_uv.hide = True
        links.new(node_uv.outputs[0], node_mapping.inputs[0])
        
        # Set default X and Y scale values to the DetailTile value.
        # Value based on pbr_std_tint_mask_det.w2mg material graph TODO check
        node_mapping.inputs[3].default_value[0] = 5
        node_mapping.inputs[3].default_value[1] = 5

    if par_name == 'rune_normal':
        node_mapping = nodes.new(type='ShaderNodeMapping')
        node_mapping.location = (-600, y_loc-200)
        node_mapping.hide = True
        node_mapping.label = "rune_normal Mapping"
        node_mapping.name = _internal_helper_node_name("Mapping", par_name)
        node_mapping.inputs[1].default_value[0] = 0.75  # X Location
        node_mapping.inputs[3].default_value[0] = 0.25  # X Scale
        links.new(node_mapping.outputs[0], node.inputs[0])

        node_uv = nodes.new(type='ShaderNodeUVMap')
        node_uv.uv_map = "SecondUV"
        node_uv.location = (node_mapping.location.x-200, node_mapping.location.y)
        node_uv.hide = True
        links.new(node_uv.outputs[0], node_mapping.inputs[0])

    if par_value.lower().endswith('.texarray'):
        texarray_source_value = par_value
        texarray_source_repo_path = normalize_depot_path(texarray_source_value)
        selected_index = _coerce_texarray_index(texarray_index)
        texarray_paths = []
        texarray_source_path = _resolve_texarray_source_path(texarray_source_value, uncook_path, repo_version)
        if texarray_source_path and os.path.exists(win_safe_path(texarray_source_path)):
            try:
                texarray_paths = convert_texarray_to_dds(texarray_source_path)
            except Exception:
                log.warning("Failed to convert texarray '%s'", texarray_source_path, exc_info=True)
        if texarray_paths and selected_index < len(texarray_paths):
            par_value = texarray_paths[selected_index]
        else:
            par_value = f"{texarray_source_value}.texture_{selected_index}{get_tex_ext(bpy.context)}"
    # We use os.path.abspath() to make sure the filepath has consistent slashes and backslashes,
    # so that we can compare image file paths to each other for duplicate checking.
    final_tex_path = par_value.replace(".xbm", get_tex_ext(bpy.context))
    try:
        final_texture = repo_file_mat(final_tex_path) # TODO fix loading texarray
        if not os.path.exists(win_safe_path(final_texture)):
            repo_texture = repo_file(final_tex_path, version=repo_version)
            if repo_texture and os.path.exists(win_safe_path(repo_texture)):
                final_texture = repo_texture
            else:
                final_texture = uncook_path + os.sep + final_tex_path
    except Exception as e:
        #raise e
        log.critical(f"TEXTURE ERROR {e}")
        final_texture= None
    
    tex_path = os.path.abspath( final_texture )
    
            
    ## didn't find the texture, try find and convert xbm
    if tex_path.lower().endswith(".xbm") and os.path.exists(win_safe_path(tex_path)):
        dds_path = os.path.splitext(tex_path)[0] + ".dds"
        if not os.path.exists(win_safe_path(dds_path)):
            try:
                convert_started = time.perf_counter()
                converted_dds_path = _convert_xbm_to_writable_dds(tex_path, dds_path)
                conversion_seconds = time.perf_counter() - convert_started
                if converted_dds_path:
                    dds_path = converted_dds_path
                    converted_from_xbm = True
            except Exception as e:
                conversion_seconds = time.perf_counter() - convert_started
                log.warning("Failed to convert xbm_to_dds: %s (%s)", tex_path, e)
        if os.path.exists(win_safe_path(dds_path)):
            tex_path = dds_path

    if not os.path.exists(win_safe_path(tex_path)):

        #check if texture and uncook path are different
        #if different change text_path to
        #dds textures should go to texture folders

        xbm_path = os.path.splitext(tex_path)[0] + ".xbm"
        dds_path = os.path.splitext(tex_path)[0] + ".dds"
        for ext in ['.tga','.dds', '.png']:
            if tex_path.endswith(ext):
                xbm_path = tex_path.replace(ext, ".xbm")
                dds_path = tex_path.replace(ext, ".dds") if ext != '.dds' else tex_path
                break
        #create dds if none exist
        if not os.path.exists(win_safe_path(dds_path)):
            if not os.path.exists(win_safe_path(xbm_path)):
                TEXTURE_PATH = get_texture_path(bpy.context)
                GAME_UNCOOK_PATH = get_uncook_path(bpy.context)
                same_root = os.path.normcase(os.path.normpath(TEXTURE_PATH)) == os.path.normcase(os.path.normpath(GAME_UNCOOK_PATH))
                if not same_root:
                    uncook_xbm_path = xbm_path.replace(TEXTURE_PATH, GAME_UNCOOK_PATH)
                    uncook_dds_path = dds_path.replace(TEXTURE_PATH, GAME_UNCOOK_PATH)
                    if os.path.exists(win_safe_path(uncook_dds_path)) or os.path.exists(win_safe_path(uncook_xbm_path)):
                        xbm_path = uncook_xbm_path
                        dds_path = uncook_dds_path
                else:
                    xbm_path = xbm_path.replace(TEXTURE_PATH, GAME_UNCOOK_PATH)
                    dds_path = dds_path.replace(TEXTURE_PATH, GAME_UNCOOK_PATH)
                # Last fallback: repo_file() will use bundle fallback extraction in uncook for textures.
                bundle_xbm_path = _resolve_existing_texture_xbm(par_value, repo_version)
                if bundle_xbm_path and os.path.exists(win_safe_path(bundle_xbm_path)):
                    xbm_path = bundle_xbm_path
                    dds_path = os.path.splitext(bundle_xbm_path)[0] + ".dds"
            if os.path.exists(win_safe_path(dds_path)):
                tex_path = dds_path
            elif os.path.exists(win_safe_path(xbm_path)):
                try:
                    convert_started = time.perf_counter()
                    converted_dds_path = _convert_xbm_to_writable_dds(xbm_path, dds_path)
                    conversion_seconds = time.perf_counter() - convert_started
                    if converted_dds_path:
                        dds_path = converted_dds_path
                        converted_from_xbm = True
                except Exception as e:
                    conversion_seconds = time.perf_counter() - convert_started
                    log.warning("Failed to convert xbm_to_dds: %s (%s)", xbm_path, e)
                if os.path.exists(win_safe_path(dds_path)):
                    tex_path = dds_path


        else:
            tex_path = dds_path

    
    texture_source_path = tex_path
    if tex_path and tex_path.lower().endswith(".dds"):
        convert_started = time.perf_counter()
        converted_image_path = _convert_dds_to_blender_image_cache(tex_path)
        conversion_seconds += time.perf_counter() - convert_started
        if converted_image_path:
            tex_path = converted_image_path
            converted_from_dds = True

    image_started = time.perf_counter()
    node.image = load_texture(
        mat,
        tex_path,
        uncook_path,
        metadata_source_path=texture_source_path if converted_from_dds else "",
    )
    image_load_seconds = time.perf_counter() - image_started
    if texarray_source_repo_path:
        node["witcher_texture_source_path"] = texarray_source_repo_path
        node["witcher_material_param_type"] = "handle:ITexture"
        try:
            node.witcher_texture_source_path = texarray_source_repo_path
        except Exception:
            pass
    if not node.image:
        node.label = "MISSING:" + par_value

    total_seconds = time.perf_counter() - texture_started
    if total_seconds >= _TEXTURE_PROFILE_WARN_THRESHOLD or converted_from_xbm or converted_from_dds:
        _log_material_profile_warning(
            "texture node %s on %s total %.3fs (convert %.3fs, load %.3fs, source %s, final %s)",
            par_name,
            getattr(mat, "name", "<material>"),
            total_seconds,
            conversion_seconds,
            image_load_seconds,
            par_value,
            tex_path,
        )

    return node


def create_node_cubemap(
        mat: Material
        ,param: Element
        ,node_ng: Node
        ,y_loc: int
        ,uncook_path: str
    ):
    """Create a ShaderNodeTexEnvironment for a handle:CCubeTexture parameter.

    Converts the .w2cube file to a cubemap DDS, loads it into an
    Environment Texture node, and connects it to the node group input.
    """
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links

    par_name = param.get('name')
    par_value = param.get('value')

    # Resolve the .w2cube file path
    repo_version = _material_repo_version(mat)
    w2cube_path = repo_file(par_value, version=repo_version) if par_value and not os.path.isabs(par_value) else par_value
    if not w2cube_path or not os.path.exists(win_safe_path(w2cube_path)):
        w2cube_path = os.path.join(uncook_path, par_value) if par_value else None

    dds_path = None
    loaded_img = None
    if w2cube_path and os.path.exists(win_safe_path(w2cube_path)):
        try:
            if par_name == 'BlickCube':
                loaded_img, dds_path = load_w2cube_blick_equirect_image(w2cube_path, colorspace='Non-Color')
            else:
                loaded_img, dds_path = load_w2cube_image(w2cube_path, colorspace='sRGB')
        except Exception as e:
            log.warning(f"Failed to convert w2cube '{par_value}': {e}")

    # Environment Texture node (Blender's cubemap-aware image node)
    node = nodes.new(type="ShaderNodeTexEnvironment")
    node.width = 300
    node.label = par_name
    cube_repo_path = normalize_depot_path(par_value)
    node["witcher_texture_source_path"] = cube_repo_path
    node["witcher_material_param_type"] = "handle:CCubeTexture"
    try:
        node.witcher_texture_source_path = cube_repo_path
    except Exception:
        pass
    if loaded_img:
        node.image = loaded_img

    if not node.image and dds_path and os.path.exists(win_safe_path(dds_path)):
        try:
            img = bpy_image_load_safe(dds_path, check_existing=True)
            if img:
                img.colorspace_settings.name = 'Non-Color' if par_name == 'BlickCube' else 'sRGB'
                node.image = img
                if par_name == 'BlickCube':
                    log.warning("BlickCube equirect build failed, falling back to DDS cubemap image: %s", dds_path)
        except Exception as e:
            log.warning(f"Failed to load cubemap DDS '{dds_path}': {e}")
    if not node.image:
        node.label = "MISSING:" + par_value

    # Add a CubeMap input to the node group if it doesn't already have one
    if not node_ng.inputs.get(par_name):
        node_tree_inputs_new(node_ng, 'NodeSocketColor', par_name)

    input_pin = node_ng.inputs.get(par_name)
    if input_pin:
        links.new(node.outputs[0], input_pin)

    return node


_EYE_BLICK_ENV_NODE = 'W3 Eye Environment Blick'
_EYE_BLICK_SCENE_PROP = 'witcher_eye_blick_color'
_EYE_IRIS_MORPH_CONTROL_NODE = 'W3 Eye Iris Morph Control'
_EYE_IRIS_MORPH_STRENGTH_NODE = 'W3 Eye Iris Morph Strength'
_EYE_IRIS_SIZE_BASE_PROP = 'witcher_eye_iris_size_base'
_EYE_IRIS_CONTROLLER_PROP = 'witcher_eye_iris_controller'


def set_eye_blick_environment_color(color, *, scene=None) -> tuple[float, float, float]:
    rgb = tuple(max(0.0, float(value)) for value in tuple(color)[:3])
    if len(rgb) != 3:
        rgb = (1.0, 1.0, 1.0)
    scene = scene or getattr(bpy.context, 'scene', None)
    if scene is not None:
        scene[_EYE_BLICK_SCENE_PROP] = rgb
    for mat in bpy.data.materials:
        tree = getattr(mat, 'node_tree', None)
        if tree is None:
            continue
        node = tree.nodes.get(_EYE_BLICK_ENV_NODE)
        if node is not None and node.type == 'VECT_MATH':
            node.inputs[1].default_value = rgb
    return rgb


def setup_eye_reflection_nodes(material: Material, nodegroup_node: Node, nodes, links):
    def find_blick_env_node():
        node = nodes.get('BlickCube')
        if node is not None and node.type == 'TEX_ENVIRONMENT':
            return node
        return next(
            (
                node for node in nodes
                if node.type == 'TEX_ENVIRONMENT'
                and (node.name == 'BlickCube' or node.label == 'BlickCube')
            ),
            None,
        )

    env_node = find_blick_env_node()
    if env_node is not None:
        _ensure_witcher3_eye_shader(nodegroup_node.node_tree)

    def ensure(name: str, node_type: str):
        node = nodes.get(name)
        if node is not None and node.bl_idname != node_type:
            nodes.remove(node)
            node = None
        if node is None:
            node = nodes.new(node_type)
            node.name = name
        return node

    def replace_input(source, target) -> None:
        while target.is_linked:
            links.remove(target.links[0])
        links.new(source, target)

    def parameter(name: str, target, default: float) -> None:
        socket = nodegroup_node.inputs.get(name)
        while target.is_linked:
            links.remove(target.links[0])
        if socket is not None and socket.is_linked:
            links.new(socket.links[0].from_socket, target)
        else:
            target.default_value = float(getattr(socket, 'default_value', default))

    for stale_name in ('eye_blick_fetch_vector', 'eye_blick_orientation'):
        stale = nodes.get(stale_name)
        if stale is not None:
            nodes.remove(stale)

    geo = ensure('W3 Eye Geometry', 'ShaderNodeNewGeometry')
    camera = ensure('W3 Eye Camera Data', 'ShaderNodeCameraData')
    texcoord = ensure('W3 Eye UV', 'ShaderNodeTexCoord')
    center_offset = ensure('W3 Eye Center Offset', 'ShaderNodeVectorMath')
    center_offset.operation = 'SCALE'
    center = ensure('W3 Eye Center', 'ShaderNodeVectorMath')
    center.operation = 'SUBTRACT'
    camera_offset = ensure('W3 Eye Camera Offset', 'ShaderNodeVectorMath')
    camera_offset.operation = 'SCALE'
    camera_position = ensure('W3 Eye Camera Position', 'ShaderNodeVectorMath')
    camera_position.operation = 'ADD'
    camera_from_center = ensure('W3 Eye Camera From Center', 'ShaderNodeVectorMath')
    camera_from_center.operation = 'SUBTRACT'
    flatten = ensure('W3 Eye Horizontal Camera', 'ShaderNodeVectorMath')
    flatten.operation = 'MULTIPLY'
    flatten.inputs[1].default_value = (1.0, 1.0, 0.0)
    side = ensure('W3 Eye Side', 'ShaderNodeVectorMath')
    side.operation = 'NORMALIZE'
    forward = ensure('W3 Eye Forward', 'ShaderNodeVectorMath')
    forward.operation = 'CROSS_PRODUCT'
    forward.inputs[1].default_value = (0.0, 0.0, 1.0)
    incident = ensure('W3 Eye Incident', 'ShaderNodeVectorMath')
    incident.operation = 'SCALE'
    incident.inputs[3].default_value = -1.0
    reflection = ensure('W3 Eye Reflection', 'ShaderNodeVectorMath')
    reflection.operation = 'REFLECT'
    lookup_x = ensure('W3 Eye Lookup X', 'ShaderNodeVectorMath')
    lookup_x.operation = 'DOT_PRODUCT'
    lookup_y = ensure('W3 Eye Lookup Y', 'ShaderNodeVectorMath')
    lookup_y.operation = 'DOT_PRODUCT'
    negate_x = ensure('W3 Eye Lookup -X', 'ShaderNodeMath')
    negate_x.operation = 'MULTIPLY'
    negate_x.inputs[1].default_value = -1.0
    reflection_xyz = ensure('W3 Eye Reflection XYZ', 'ShaderNodeSeparateXYZ')
    lookup = ensure('W3 Eye Blick Lookup', 'ShaderNodeCombineXYZ')

    replace_input(geo.outputs['Normal'], center_offset.inputs[0])
    parameter('EyeRadius', center_offset.inputs[3], 0.015)
    replace_input(geo.outputs['Position'], center.inputs[0])
    replace_input(center_offset.outputs['Vector'], center.inputs[1])
    replace_input(geo.outputs['Incoming'], camera_offset.inputs[0])
    replace_input(camera.outputs['View Distance'], camera_offset.inputs[3])
    replace_input(geo.outputs['Position'], camera_position.inputs[0])
    replace_input(camera_offset.outputs['Vector'], camera_position.inputs[1])
    replace_input(camera_position.outputs['Vector'], camera_from_center.inputs[0])
    replace_input(center.outputs['Vector'], camera_from_center.inputs[1])
    replace_input(camera_from_center.outputs['Vector'], flatten.inputs[0])
    replace_input(flatten.outputs['Vector'], side.inputs[0])
    replace_input(side.outputs['Vector'], forward.inputs[0])
    replace_input(geo.outputs['Incoming'], incident.inputs[0])
    replace_input(incident.outputs['Vector'], reflection.inputs[0])
    replace_input(geo.outputs['Normal'], reflection.inputs[1])
    replace_input(forward.outputs['Vector'], lookup_x.inputs[0])
    replace_input(reflection.outputs['Vector'], lookup_x.inputs[1])
    replace_input(side.outputs['Vector'], lookup_y.inputs[0])
    replace_input(reflection.outputs['Vector'], lookup_y.inputs[1])
    replace_input(lookup_x.outputs['Value'], negate_x.inputs[0])
    replace_input(reflection.outputs['Vector'], reflection_xyz.inputs[0])
    # w2cube conversion axes: RED (forward, side, up) -> Blender (side, -forward, up).
    replace_input(lookup_y.outputs['Value'], lookup.inputs['X'])
    replace_input(negate_x.outputs[0], lookup.inputs['Y'])
    replace_input(reflection_xyz.outputs['Z'], lookup.inputs['Z'])
    if env_node is not None:
        replace_input(lookup.outputs['Vector'], env_node.inputs['Vector'])

    uv_xyz = ensure('W3 Eye UV XYZ', 'ShaderNodeSeparateXYZ')
    uv_positive = ensure('W3 Eye UV Positive', 'ShaderNodeMath')
    uv_positive.operation = 'GREATER_THAN'
    uv_twice = ensure('W3 Eye UV Tile Offset', 'ShaderNodeMath')
    uv_twice.operation = 'MULTIPLY'
    uv_twice.inputs[1].default_value = 2.0
    uv_plus_one = ensure('W3 Eye UV Plus One', 'ShaderNodeMath')
    uv_plus_one.operation = 'ADD'
    uv_plus_one.inputs[1].default_value = 1.0
    uv_wrapped = ensure('W3 Eye UV Wrapped', 'ShaderNodeMath')
    uv_wrapped.operation = 'SUBTRACT'
    corner_mask = ensure('W3 Eye Corner Meat Mask', 'ShaderNodeMath')
    corner_mask.operation = 'LESS_THAN'
    replace_input(texcoord.outputs['UV'], uv_xyz.inputs[0])
    replace_input(uv_xyz.outputs['X'], uv_positive.inputs[0])
    replace_input(uv_positive.outputs[0], uv_twice.inputs[0])
    replace_input(uv_xyz.outputs['X'], uv_plus_one.inputs[0])
    replace_input(uv_plus_one.outputs[0], uv_wrapped.inputs[0])
    replace_input(uv_twice.outputs[0], uv_wrapped.inputs[1])
    replace_input(uv_wrapped.outputs[0], corner_mask.inputs[0])

    # pbr_eye reflects the Blick cubemap around its procedural corneal normal,
    # not the mesh normal used above to recover the eye centre.
    bubble_uv = ensure('W3 Eye Bubble UV', 'ShaderNodeCombineXYZ')
    replace_input(uv_wrapped.outputs[0], bubble_uv.inputs['X'])
    replace_input(uv_xyz.outputs['Y'], bubble_uv.inputs['Y'])
    bubble_uv_tiled = ensure('W3 Eye Bubble UV Tiled', 'ShaderNodeVectorMath')
    bubble_uv_tiled.operation = 'SCALE'
    replace_input(bubble_uv.outputs['Vector'], bubble_uv_tiled.inputs[0])
    parameter('BubbleNormalTile', bubble_uv_tiled.inputs[3], 10.0)

    bubble_texture = nodegroup_node.inputs.get('NormalBubble')
    bubble_color = bubble_texture.links[0].from_socket if bubble_texture and bubble_texture.is_linked else None
    if bubble_color is not None:
        vector_input = bubble_color.node.inputs.get('Vector')
        if vector_input is not None:
            replace_input(bubble_uv_tiled.outputs['Vector'], vector_input)

    bubble_unpack_scale = ensure('W3 Eye Bubble Detail x2', 'ShaderNodeVectorMath')
    bubble_unpack_scale.operation = 'SCALE'
    bubble_unpack_scale.inputs[3].default_value = 2.0
    bubble_unpack = ensure('W3 Eye Bubble Detail Unpack', 'ShaderNodeVectorMath')
    bubble_unpack.operation = 'ADD'
    bubble_unpack.inputs[1].default_value = (-1.0, -1.0, -1.0)
    if bubble_color is not None:
        replace_input(bubble_color, bubble_unpack_scale.inputs[0])
    else:
        bubble_unpack_scale.inputs[0].default_value = (0.5, 0.5, 1.0)
    replace_input(bubble_unpack_scale.outputs['Vector'], bubble_unpack.inputs[0])

    bubble_offset = ensure('W3 Eye Bubble Offset', 'ShaderNodeVectorMath')
    bubble_offset.operation = 'SUBTRACT'
    bubble_offset.inputs[1].default_value = (0.5, 0.5, 0.0)
    replace_input(bubble_uv.outputs['Vector'], bubble_offset.inputs[0])
    bubble_offset_length = ensure('W3 Eye Bubble Offset Length', 'ShaderNodeVectorMath')
    bubble_offset_length.operation = 'LENGTH'
    replace_input(bubble_offset.outputs['Vector'], bubble_offset_length.inputs[0])

    iris_low = ensure('W3 Eye Iris Low', 'ShaderNodeMath')
    iris_low.operation = 'SUBTRACT'
    parameter('IrisCoordFactor', iris_low.inputs[0], 0.1)
    parameter('IrisCoordMargin', iris_low.inputs[1], 0.02)
    iris_width = ensure('W3 Eye Iris Width', 'ShaderNodeMath')
    iris_width.operation = 'MULTIPLY'
    iris_width.inputs[1].default_value = 2.0
    parameter('IrisCoordMargin', iris_width.inputs[0], 0.02)
    iris_distance = ensure('W3 Eye Iris Distance', 'ShaderNodeMath')
    iris_distance.operation = 'SUBTRACT'
    replace_input(bubble_offset_length.outputs['Value'], iris_distance.inputs[0])
    replace_input(iris_low.outputs[0], iris_distance.inputs[1])
    iris_ramp = ensure('W3 Eye Iris Ramp', 'ShaderNodeMath')
    iris_ramp.operation = 'DIVIDE'
    replace_input(iris_distance.outputs[0], iris_ramp.inputs[0])
    replace_input(iris_width.outputs[0], iris_ramp.inputs[1])
    iris_invert = ensure('W3 Eye Iris Invert', 'ShaderNodeMath')
    iris_invert.operation = 'SUBTRACT'
    iris_invert.inputs[0].default_value = 1.0
    replace_input(iris_ramp.outputs[0], iris_invert.inputs[1])
    iris_factor = ensure('W3 Eye Iris Factor', 'ShaderNodeClamp')
    iris_factor.inputs['Min'].default_value = 0.0
    iris_factor.inputs['Max'].default_value = 1.0
    replace_input(iris_invert.outputs[0], iris_factor.inputs['Value'])

    # EyeRaytrace applies IrisSize to both Diffuse and NormalBase UVs.
    iris_size_base = float(material.get(_EYE_IRIS_SIZE_BASE_PROP, 0.0) or 0.0)
    if iris_size_base <= 0.0:
        iris_size_socket = nodegroup_node.inputs.get('IrisSize')
        iris_size_base = 0.65
        if iris_size_socket is not None:
            if iris_size_socket.is_linked:
                source_socket = iris_size_socket.links[0].from_socket
                iris_size_base = float(getattr(source_socket, 'default_value', iris_size_base))
            else:
                iris_size_base = float(getattr(iris_size_socket, 'default_value', iris_size_base))
        iris_size_base = max(0.0001, iris_size_base)
        material[_EYE_IRIS_SIZE_BASE_PROP] = iris_size_base

    iris_size_relative = ensure('W3 Eye Iris Size Relative', 'ShaderNodeMath')
    iris_size_relative.operation = 'DIVIDE'
    parameter('IrisSize', iris_size_relative.inputs[0], iris_size_base)
    iris_size_relative.inputs[1].default_value = iris_size_base

    iris_morph_control = ensure(_EYE_IRIS_MORPH_CONTROL_NODE, 'ShaderNodeValue')
    iris_morph_control.outputs[0].default_value = 0.0
    existing_strength = nodes.get(_EYE_IRIS_MORPH_STRENGTH_NODE)
    iris_morph_strength = ensure(_EYE_IRIS_MORPH_STRENGTH_NODE, 'ShaderNodeValue')
    if existing_strength is None or existing_strength is not iris_morph_strength:
        # No native bone-to-material calibration exists; keep this editable.
        iris_morph_strength.outputs[0].default_value = 0.2
    iris_morph_amount = ensure('W3 Eye Iris Morph Amount', 'ShaderNodeMath')
    iris_morph_amount.operation = 'MULTIPLY'
    replace_input(iris_morph_control.outputs[0], iris_morph_amount.inputs[0])
    replace_input(iris_morph_strength.outputs[0], iris_morph_amount.inputs[1])
    iris_morph_scale = ensure('W3 Eye Iris Morph Scale', 'ShaderNodeMath')
    iris_morph_scale.operation = 'ADD'
    iris_morph_scale.inputs[0].default_value = 1.0
    replace_input(iris_morph_amount.outputs[0], iris_morph_scale.inputs[1])
    iris_scale = ensure('W3 Eye Iris Scale', 'ShaderNodeMath')
    iris_scale.operation = 'MULTIPLY'
    replace_input(iris_size_relative.outputs[0], iris_scale.inputs[0])
    replace_input(iris_morph_scale.outputs[0], iris_scale.inputs[1])

    iris_centered = ensure('W3 Eye Iris UV Centered', 'ShaderNodeVectorMath')
    iris_centered.operation = 'SUBTRACT'
    iris_centered.inputs[1].default_value = (0.5, 0.5, 0.0)
    replace_input(bubble_uv.outputs['Vector'], iris_centered.inputs[0])
    iris_scaled = ensure('W3 Eye Iris UV Scaled', 'ShaderNodeVectorMath')
    iris_scaled.operation = 'SCALE'
    replace_input(iris_centered.outputs['Vector'], iris_scaled.inputs[0])
    replace_input(iris_scale.outputs[0], iris_scaled.inputs[3])
    iris_recentered = ensure('W3 Eye Iris UV Recentered', 'ShaderNodeVectorMath')
    iris_recentered.operation = 'ADD'
    iris_recentered.inputs[1].default_value = (0.5, 0.5, 0.0)
    replace_input(iris_scaled.outputs['Vector'], iris_recentered.inputs[0])
    iris_uv_delta = ensure('W3 Eye Iris UV Delta', 'ShaderNodeVectorMath')
    iris_uv_delta.operation = 'SUBTRACT'
    replace_input(iris_recentered.outputs['Vector'], iris_uv_delta.inputs[0])
    replace_input(bubble_uv.outputs['Vector'], iris_uv_delta.inputs[1])
    iris_uv_masked = ensure('W3 Eye Iris UV Masked', 'ShaderNodeVectorMath')
    iris_uv_masked.operation = 'SCALE'
    replace_input(iris_uv_delta.outputs['Vector'], iris_uv_masked.inputs[0])
    replace_input(iris_factor.outputs['Result'], iris_uv_masked.inputs[3])
    iris_uv = ensure('W3 Eye Iris UV', 'ShaderNodeVectorMath')
    iris_uv.operation = 'ADD'
    replace_input(bubble_uv.outputs['Vector'], iris_uv.inputs[0])
    replace_input(iris_uv_masked.outputs['Vector'], iris_uv.inputs[1])

    for texture_name in ('Diffuse', 'NormalBase'):
        texture_socket = nodegroup_node.inputs.get(texture_name)
        if texture_socket is None or not texture_socket.is_linked:
            continue
        texture_node = texture_socket.links[0].from_node
        vector_input = texture_node.inputs.get('Vector')
        if texture_node.type == 'TEX_IMAGE' and vector_input is not None:
            replace_input(iris_uv.outputs['Vector'], vector_input)

    bubble_detail_factor = ensure('W3 Eye Bubble Detail Factor', 'ShaderNodeMath')
    bubble_detail_factor.operation = 'SUBTRACT'
    bubble_detail_factor.inputs[0].default_value = 1.0
    replace_input(iris_factor.outputs['Result'], bubble_detail_factor.inputs[1])

    bubble_radius_below = ensure('W3 Eye Bubble Radius Below', 'ShaderNodeMath')
    bubble_radius_below.operation = 'MULTIPLY'
    parameter('EggFullRadius', bubble_radius_below.inputs[0], 0.5)
    parameter('EggSubFactor', bubble_radius_below.inputs[1], 0.22)
    bubble_offset_xyz = ensure('W3 Eye Bubble Offset XYZ', 'ShaderNodeSeparateXYZ')
    replace_input(bubble_offset.outputs['Vector'], bubble_offset_xyz.inputs[0])
    bubble_v0 = ensure('W3 Eye Bubble V0', 'ShaderNodeCombineXYZ')
    replace_input(bubble_offset_xyz.outputs['X'], bubble_v0.inputs['X'])
    replace_input(bubble_offset_xyz.outputs['Y'], bubble_v0.inputs['Y'])
    replace_input(bubble_radius_below.outputs[0], bubble_v0.inputs['Z'])
    bubble_v0_normalized = ensure('W3 Eye Bubble V0 Normalized', 'ShaderNodeVectorMath')
    bubble_v0_normalized.operation = 'NORMALIZE'
    replace_input(bubble_v0.outputs['Vector'], bubble_v0_normalized.inputs[0])
    bubble_v1 = ensure('W3 Eye Bubble V1', 'ShaderNodeVectorMath')
    bubble_v1.operation = 'SCALE'
    replace_input(bubble_v0_normalized.outputs['Vector'], bubble_v1.inputs[0])
    parameter('EggFullRadius', bubble_v1.inputs[3], 0.5)
    bubble_delta = ensure('W3 Eye Bubble Delta', 'ShaderNodeVectorMath')
    bubble_delta.operation = 'SUBTRACT'
    replace_input(bubble_v1.outputs['Vector'], bubble_delta.inputs[0])
    replace_input(bubble_v0.outputs['Vector'], bubble_delta.inputs[1])
    bubble_delta_normalized = ensure('W3 Eye Bubble Delta Normalized', 'ShaderNodeVectorMath')
    bubble_delta_normalized.operation = 'NORMALIZE'
    replace_input(bubble_delta.outputs['Vector'], bubble_delta_normalized.inputs[0])
    bubble_v1_xyz = ensure('W3 Eye Bubble V1 XYZ', 'ShaderNodeSeparateXYZ')
    replace_input(bubble_v1.outputs['Vector'], bubble_v1_xyz.inputs[0])
    bubble_v0_xyz = ensure('W3 Eye Bubble V0 XYZ', 'ShaderNodeSeparateXYZ')
    replace_input(bubble_v0.outputs['Vector'], bubble_v0_xyz.inputs[0])
    bubble_above = ensure('W3 Eye Bubble Above', 'ShaderNodeMath')
    bubble_above.operation = 'GREATER_THAN'
    replace_input(bubble_v1_xyz.outputs['Z'], bubble_above.inputs[0])
    replace_input(bubble_v0_xyz.outputs['Z'], bubble_above.inputs[1])
    bubble_delta_selected = ensure('W3 Eye Bubble Delta Selected', 'ShaderNodeVectorMath')
    bubble_delta_selected.operation = 'SCALE'
    replace_input(bubble_delta_normalized.outputs['Vector'], bubble_delta_selected.inputs[0])
    replace_input(bubble_above.outputs[0], bubble_delta_selected.inputs[3])
    bubble_not_above = ensure('W3 Eye Bubble Not Above', 'ShaderNodeMath')
    bubble_not_above.operation = 'SUBTRACT'
    bubble_not_above.inputs[0].default_value = 1.0
    replace_input(bubble_above.outputs[0], bubble_not_above.inputs[1])
    bubble_up = ensure('W3 Eye Bubble Up', 'ShaderNodeCombineXYZ')
    replace_input(bubble_not_above.outputs[0], bubble_up.inputs['Z'])
    bubble_base_normal = ensure('W3 Eye Bubble Base Normal', 'ShaderNodeVectorMath')
    bubble_base_normal.operation = 'ADD'
    replace_input(bubble_delta_selected.outputs['Vector'], bubble_base_normal.inputs[0])
    replace_input(bubble_up.outputs['Vector'], bubble_base_normal.inputs[1])

    bubble_radius_squared = ensure('W3 Eye Bubble Radius Squared', 'ShaderNodeMath')
    bubble_radius_squared.operation = 'MULTIPLY'
    parameter('EggFullRadius', bubble_radius_squared.inputs[0], 0.5)
    parameter('EggFullRadius', bubble_radius_squared.inputs[1], 0.5)
    bubble_below_squared = ensure('W3 Eye Bubble Below Squared', 'ShaderNodeMath')
    bubble_below_squared.operation = 'MULTIPLY'
    replace_input(bubble_radius_below.outputs[0], bubble_below_squared.inputs[0])
    replace_input(bubble_radius_below.outputs[0], bubble_below_squared.inputs[1])
    bubble_max_offset_squared = ensure('W3 Eye Bubble Max Offset Squared', 'ShaderNodeMath')
    bubble_max_offset_squared.operation = 'SUBTRACT'
    replace_input(bubble_radius_squared.outputs[0], bubble_max_offset_squared.inputs[0])
    replace_input(bubble_below_squared.outputs[0], bubble_max_offset_squared.inputs[1])
    bubble_max_offset = ensure('W3 Eye Bubble Max Offset', 'ShaderNodeMath')
    bubble_max_offset.operation = 'SQRT'
    replace_input(bubble_max_offset_squared.outputs[0], bubble_max_offset.inputs[0])
    bubble_edge_ratio = ensure('W3 Eye Bubble Edge Ratio', 'ShaderNodeMath')
    bubble_edge_ratio.operation = 'DIVIDE'
    replace_input(bubble_offset_length.outputs['Value'], bubble_edge_ratio.inputs[0])
    replace_input(bubble_max_offset.outputs[0], bubble_edge_ratio.inputs[1])
    bubble_edge_scaled = ensure('W3 Eye Bubble Edge Scaled', 'ShaderNodeMath')
    bubble_edge_scaled.operation = 'MULTIPLY'
    replace_input(bubble_edge_ratio.outputs[0], bubble_edge_scaled.inputs[0])
    parameter('EggMarginFactor', bubble_edge_scaled.inputs[1], 1.0)
    bubble_edge_invert = ensure('W3 Eye Bubble Edge Invert', 'ShaderNodeMath')
    bubble_edge_invert.operation = 'SUBTRACT'
    bubble_edge_invert.inputs[0].default_value = 1.0
    replace_input(bubble_edge_scaled.outputs[0], bubble_edge_invert.inputs[1])
    bubble_edge_clamp = ensure('W3 Eye Bubble Edge Clamp', 'ShaderNodeClamp')
    bubble_edge_clamp.inputs['Min'].default_value = 0.0
    bubble_edge_clamp.inputs['Max'].default_value = 1.0
    replace_input(bubble_edge_invert.outputs[0], bubble_edge_clamp.inputs['Value'])
    bubble_edge = ensure('W3 Eye Bubble Edge', 'ShaderNodeMath')
    bubble_edge.operation = 'POWER'
    replace_input(bubble_edge_clamp.outputs['Result'], bubble_edge.inputs[0])
    parameter('EggMarginExponent', bubble_edge.inputs[1], 1.6)
    bubble_edge_xy = ensure('W3 Eye Bubble Edge XY', 'ShaderNodeCombineXYZ')
    replace_input(bubble_edge.outputs[0], bubble_edge_xy.inputs['X'])
    replace_input(bubble_edge.outputs[0], bubble_edge_xy.inputs['Y'])
    bubble_edge_xy.inputs['Z'].default_value = 1.0
    bubble_rounded = ensure('W3 Eye Bubble Rounded', 'ShaderNodeVectorMath')
    bubble_rounded.operation = 'MULTIPLY'
    replace_input(bubble_base_normal.outputs['Vector'], bubble_rounded.inputs[0])
    replace_input(bubble_edge_xy.outputs['Vector'], bubble_rounded.inputs[1])
    bubble_rounded_normalized = ensure('W3 Eye Bubble Rounded Normalized', 'ShaderNodeVectorMath')
    bubble_rounded_normalized.operation = 'NORMALIZE'
    replace_input(bubble_rounded.outputs['Vector'], bubble_rounded_normalized.inputs[0])

    bubble_detail_xy = ensure('W3 Eye Bubble Detail XY', 'ShaderNodeVectorMath')
    bubble_detail_xy.operation = 'MULTIPLY'
    # Blender's V flip requires inverting sampled normal-detail Y.
    bubble_detail_xy.inputs[1].default_value = (1.0, -1.0, 0.0)
    replace_input(bubble_unpack.outputs['Vector'], bubble_detail_xy.inputs[0])
    bubble_detail_scaled = ensure('W3 Eye Bubble Detail Scaled', 'ShaderNodeVectorMath')
    bubble_detail_scaled.operation = 'SCALE'
    replace_input(bubble_detail_xy.outputs['Vector'], bubble_detail_scaled.inputs[0])
    replace_input(bubble_detail_factor.outputs[0], bubble_detail_scaled.inputs[3])
    bubble_tangent = ensure('W3 Eye Bubble Tangent Normal', 'ShaderNodeVectorMath')
    bubble_tangent.operation = 'ADD'
    replace_input(bubble_rounded_normalized.outputs['Vector'], bubble_tangent.inputs[0])
    replace_input(bubble_detail_scaled.outputs['Vector'], bubble_tangent.inputs[1])
    bubble_tangent_normalized = ensure('W3 Eye Bubble Tangent Normalized', 'ShaderNodeVectorMath')
    bubble_tangent_normalized.operation = 'NORMALIZE'
    replace_input(bubble_tangent.outputs['Vector'], bubble_tangent_normalized.inputs[0])
    bubble_encoded_scale = ensure('W3 Eye Bubble Encode Scale', 'ShaderNodeVectorMath')
    bubble_encoded_scale.operation = 'SCALE'
    bubble_encoded_scale.inputs[3].default_value = 0.5
    replace_input(bubble_tangent_normalized.outputs['Vector'], bubble_encoded_scale.inputs[0])
    bubble_encoded = ensure('W3 Eye Bubble Encoded', 'ShaderNodeVectorMath')
    bubble_encoded.operation = 'ADD'
    bubble_encoded.inputs[1].default_value = (0.5, 0.5, 0.5)
    replace_input(bubble_encoded_scale.outputs['Vector'], bubble_encoded.inputs[0])
    bubble_world = ensure('W3 Eye Bubble World Normal', 'ShaderNodeNormalMap')
    bubble_world.space = 'TANGENT'
    replace_input(bubble_encoded.outputs['Vector'], bubble_world.inputs['Color'])
    replace_input(bubble_world.outputs['Normal'], reflection.inputs[1])

    if env_node is None:
        return

    def masked_parameter(prefix: str, iris_name: str, meat_name: str, default: float):
        delta = ensure(f'W3 Eye {prefix} Delta', 'ShaderNodeMath')
        delta.operation = 'SUBTRACT'
        masked = ensure(f'W3 Eye {prefix} Masked', 'ShaderNodeMath')
        masked.operation = 'MULTIPLY'
        value = ensure(f'W3 Eye {prefix}', 'ShaderNodeMath')
        value.operation = 'ADD'
        parameter(meat_name, delta.inputs[0], default)
        parameter(iris_name, delta.inputs[1], default)
        replace_input(delta.outputs[0], masked.inputs[0])
        replace_input(corner_mask.outputs[0], masked.inputs[1])
        parameter(iris_name, value.inputs[0], default)
        replace_input(masked.outputs[0], value.inputs[1])
        return value.outputs[0]

    blick_scale = masked_parameter('Blick Scale', 'BlickScale', 'BlikScaleMeat', 1.0)
    specularity = masked_parameter('Specularity', 'Specularity', 'SpecularityMeat', 0.18)
    scaled_blick = ensure('W3 Eye Scaled Blick', 'ShaderNodeVectorMath')
    scaled_blick.operation = 'SCALE'
    replace_input(env_node.outputs['Color'], scaled_blick.inputs[0])
    replace_input(blick_scale, scaled_blick.inputs[3])

    environment_blick = ensure(_EYE_BLICK_ENV_NODE, 'ShaderNodeVectorMath')
    environment_blick.operation = 'MULTIPLY'
    replace_input(scaled_blick.outputs['Vector'], environment_blick.inputs[0])
    scene = getattr(bpy.context, 'scene', None)
    environment_color = tuple(scene.get(_EYE_BLICK_SCENE_PROP, (1.0, 1.0, 1.0))) if scene else (1.0,) * 3
    environment_blick.inputs[1].default_value = environment_color[:3]
    replace_input(environment_blick.outputs['Vector'], nodegroup_node.inputs['BlickCube'])

    specular_linear = ensure('W3 Eye Specularity Linear', 'ShaderNodeMath')
    specular_linear.operation = 'POWER'
    specular_linear.inputs[1].default_value = 2.2
    specular_level = ensure('W3 Eye Specular IOR Level', 'ShaderNodeMath')
    specular_level.operation = 'MULTIPLY'
    specular_level.inputs[1].default_value = 1.0 / (2.0 * ((1.38 - 1.0) / (1.38 + 1.0)) ** 2)
    replace_input(specularity, specular_linear.inputs[0])
    replace_input(specular_linear.outputs[0], specular_level.inputs[0])
    replace_input(specular_level.outputs[0], nodegroup_node.inputs['Specular'])

    env_node.label = 'REDengine BlickCube'
    env_node.interpolation = 'Linear'
    if getattr(env_node, 'image', None) is not None:
        try:
            if env_node.image.get('witcher_blick_equirect_dds'):
                env_node.projection = 'EQUIRECTANGULAR'
                env_node.image.colorspace_settings.name = 'Non-Color'
        except Exception:
            pass


def _find_eye_shader_group_node(material):
    tree = getattr(material, 'node_tree', None)
    if tree is None:
        return None
    for node in tree.nodes:
        if node.type != 'GROUP' or node.node_tree is None:
            continue
        if _node_group_family_name(node.node_tree.name).lower() == 'witcher3_eye':
            return node
    return None


def setup_eye_iris_morph_drivers(mesh_objects, control_armature=None, control_bone_name='w3_face_poses') -> int:
    mesh_objects = [
        mesh_obj
        for mesh_obj in (mesh_objects or [])
        if mesh_obj is not None and getattr(mesh_obj, 'type', None) == 'MESH'
    ]
    mesh_pointers = {mesh_obj.as_pointer() for mesh_obj in mesh_objects}
    material_sources = {}
    for mesh_obj in mesh_objects:
        for slot in getattr(mesh_obj, 'material_slots', []):
            material = getattr(slot, 'material', None)
            if material is not None:
                material_sources.setdefault(material.as_pointer(), (material, mesh_obj))

    driver_count = 0
    for material, mesh_obj in material_sources.values():
        nodegroup_node = _find_eye_shader_group_node(material)
        if nodegroup_node is None:
            continue

        shape_keys = getattr(getattr(mesh_obj, 'data', None), 'shape_keys', None)
        key_blocks = getattr(shape_keys, 'key_blocks', None)
        pose = getattr(control_armature, 'pose', None)
        pose_bone = pose.bones.get(control_bone_name) if pose is not None else None
        use_armature = bool(
            pose_bone is not None
            and 'iris_wide' in pose_bone
            and 'iris_narrow' in pose_bone
        )
        use_shape_keys = not use_armature and bool(
            key_blocks
            and key_blocks.get('iris_wide') is not None
            and key_blocks.get('iris_narrow') is not None
        )
        if not use_armature and not use_shape_keys:
            continue

        controller_name = (
            getattr(control_armature, 'name_full', getattr(control_armature, 'name', ''))
            if use_armature
            else shape_keys.name
        )
        previous_controller = str(material.get(_EYE_IRIS_CONTROLLER_PROP, '') or '')
        has_external_owner = any(
            obj.as_pointer() not in mesh_pointers
            and getattr(obj, 'type', None) == 'MESH'
            and any(slot.material is material for slot in obj.material_slots)
            for obj in bpy.data.objects
        )
        if (previous_controller and previous_controller != controller_name) or (
            not previous_controller and has_external_owner
        ):
            original_material = material
            material = original_material.copy()
            for current_mesh in mesh_objects:
                for slot in current_mesh.material_slots:
                    if slot.material is original_material:
                        slot.material = material
            nodegroup_node = _find_eye_shader_group_node(material)
        material[_EYE_IRIS_CONTROLLER_PROP] = controller_name

        tree = material.node_tree
        setup_eye_reflection_nodes(material, nodegroup_node, tree.nodes, tree.links)
        control_node = tree.nodes.get(_EYE_IRIS_MORPH_CONTROL_NODE)
        if control_node is None or control_node.type != 'VALUE':
            continue

        driver_curve = control_node.outputs[0].driver_add('default_value')
        driver = driver_curve.driver
        driver.type = 'SCRIPTED'
        while driver.variables:
            driver.variables.remove(driver.variables[0])
        for variable_name, channel_name in (('wide', 'iris_wide'), ('narrow', 'iris_narrow')):
            variable = driver.variables.new()
            variable.name = variable_name
            variable.type = 'SINGLE_PROP'
            target = variable.targets[0]
            if use_armature:
                target.id_type = 'OBJECT'
                target.id = control_armature
                target.data_path = f'pose.bones["{control_bone_name}"]["{channel_name}"]'
            else:
                target.id_type = 'KEY'
                target.id = shape_keys
                target.data_path = f'key_blocks["{channel_name}"].value'
        driver.expression = 'narrow - wide'
        tree.update_tag()
        driver_count += 1

    return driver_count


def load_texture(
        mat: Material
        ,tex_path: str
        ,uncook_path: str
        ,metadata_source_path: str = ""
    ) -> Image:
    texture_started = time.perf_counter()
    tex_path = win_unprefix_path(tex_path)
    metadata_source_path = win_unprefix_path(metadata_source_path or tex_path)
    if tex_path and tex_path.lower().endswith(".dds"):
        converted_image_path = _convert_dds_to_blender_image_cache(tex_path)
        if converted_image_path:
            metadata_source_path = metadata_source_path or tex_path
            tex_path = win_unprefix_path(converted_image_path)
    img_filename = os.path.basename(tex_path)	# Filename with extension.
    overwrite_existing = overwrite_existing_enabled()

    # Check if an image with this filepath is already loaded.
    img = None
    tex_key = win_path_key(tex_path)
    for i in bpy.data.images:
        #if bpy.path.basename(i.filepath) == img_filename:
        if win_path_key(i.filepath) == tex_key:
            img = i
            break
    if img and tex_path.lower().endswith('.dds') and overwrite_existing:
        repaired_img, load_error = load_image_with_dds_repair(
            tex_path,
            image=img,
            check_existing=True,
            allow_dds_repair=True,
        )
        if repaired_img is not None:
            img = repaired_img
        else:
            log.warning("Failed to refresh cached DDS image %s: %s", tex_path, load_error)
    # Check if the file exists
    if not img and not os.path.isfile(win_safe_path(tex_path)):
        log.info("Image not found: " + tex_path + " (Usually unimportant)")

        img = bpy.data.images.new(img_filename, width=1024, height=1024)
        img.filepath = tex_path
        img.source = 'FILE'
        #return
    elif not img:
        if tex_path.lower().endswith('.dds'):
            img, load_error = load_image_with_dds_repair(
                tex_path,
                check_existing=True,
                allow_dds_repair=overwrite_existing,
            )
            if img is None:
                log.warning("Failed to load image %s: %s", tex_path, load_error)
                img = bpy.data.images.new(img_filename, width=1024, height=1024)
                img.filepath = tex_path
                img.source = 'FILE'
        else:
            img = bpy_image_load_safe(tex_path, check_existing=True)

    # Correct the image name.
    display_filepath = metadata_source_path or img.filepath
    filepath = display_filepath.replace(os.sep, "/")
    filename = filepath.split("/")[-1]
    file_parts = filename.split(".")
    img_name = file_parts[0]
    # if 'texarray' in filepath:
    #     # Add the texture number at the end.
    #     end = file_parts[-2]
    #     img_name += end.split("texture")[1]
    img.name = img_name

    if tex_path.lower().endswith('.dds') or metadata_source_path.lower().endswith('.dds'):
        img.alpha_mode = 'CHANNEL_PACKED'

    if metadata_source_path and metadata_source_path != tex_path:
        try:
            img["witcher_original_texture_path"] = metadata_source_path
            img["witcher_cached_texture_path"] = tex_path
        except Exception:
            pass

    _apply_image_texture_metadata(img, metadata_source_path or tex_path)

    total_seconds = time.perf_counter() - texture_started
    if total_seconds >= _TEXTURE_PROFILE_WARN_THRESHOLD:
        _log_material_profile_warning(
            "load texture %s total %.3fs (material %s, exists %s)",
            tex_path,
            total_seconds,
            getattr(mat, "name", "<material>"),
            "yes" if os.path.isfile(win_safe_path(tex_path)) else "no",
        )

    return img

def create_node_float(mat, param, node_ng):
    nodes = mat.node_tree.nodes
    par_name = param.get('name')
    par_value = param.get('value')

    # if 'Rotation' in par_name:
    #     normal_node = nodes.get(par_name.replace('Rotation', 'Normal'))
    #     if normal_node != None:
    #         mapping_node = normal_node.inputs[0].links[0].from_node
    #         # Set Z rotation
    #         mapping_node.inputs[1].default_value[2] = float(par_value)
    #         return
    node = nodes.new(type='ShaderNodeValue')
    node.outputs[0].default_value = float(par_value)

    return node

def create_node_color(mat, param, node_ng):
    nodes = mat.node_tree.nodes
    par_value = param.get('value')

    values = [float(f) for f in par_value.split("; ")]
    node = nodes.new(type='ShaderNodeRGB')
    node.outputs[0].default_value = (
        values[0] / 255
        ,values[1] / 255
        ,values[2] / 255
        ,values[3] / 255
    )

    return node

def create_node_vector(mat, param, node_ng, do_vec_4 = False):
    nodes = mat.node_tree.nodes
    par_name = param.get('name')
    par_value = param.get('value')

    values = [float(f) for f in par_value.split("; ")]

    node = nodes.new(type='ShaderNodeCombineXYZ')
    node.inputs[0].default_value = values[0]
    node.inputs[1].default_value = values[1]
    node.inputs[2].default_value = values[2]
    mark_vector_param_node(
        node,
        par_name,
        values[3] if len(values) > 3 else 1.0,
        VECTOR_SOURCE_XYZ,
    )
    _apply_uv_mapping_vector_links(mat, par_name, node)

    if do_vec_4:
        return node
    return node

def create_node_attribute(mat, param, node_ng):
    nodes = mat.node_tree.nodes
    par_value = param.get('value')

    node = nodes.new(type="ShaderNodeAttribute")
    node.attribute_name = par_value

    return node

def mat_ensure_dummy_transparent_img_node(material, node_ng, shader_type, nodes):
    """If the material doesn't have a diffuse texture, but has a shader that supports transparency
    (likely glass or water), let's add a transparent image node, to make the material appear nicer
    in textured viewport.
    """
    if node_ng.node_tree.name not in ['Witcher3_Glass', 'Invisible']:
        # If this isn't a material that should be fully transparent, do nothing.
        return
    if node_ng and len(node_ng.inputs) > 0 and len(node_ng.inputs[0].links) > 0:
        # If there is already a diffuse texture, do nothing.
        return

    transp_img = bpy.data.images.get('Transparent')
    if not transp_img:
        # Create the transparent image for the first time.
        bpy.ops.image.new(name="Transparent", width=64, height=64, color=(0, 0, 0, 0), alpha=True)
        transp_img = bpy.data.images['Transparent']

    node = nodes.new(type='ShaderNodeTexImage')
    node.image = transp_img
    node.width = 300
    node.location = (-600, 1000+320)
    nodes.active = node

def mat_set_name_by_diffuse(mat, node_ng, nodes):
    """Set the material's name to the name of the diffuse texture.
    Also set the diffuse texture's node as the active node, for Textured Viewport shading.
    """

    if node_ng.node_tree.name == 'Invisible':
        mat.name = 'Invisible'
        return

    named = False
    for inp in node_ng.inputs:
        if len(inp.links) == 0:
            continue
        from_node = inp.links[0].from_node
        if from_node.type == 'TEX_IMAGE' and from_node.image:
            img_name = from_node.image.name
            if img_name.endswith("_d0") or img_name.endswith("_n0"):
                mat.name = img_name[:-3]
            elif img_name.endswith("_d") or img_name.endswith("_n"):
                mat.name = img_name[:-2]
            else:
                mat.name = img_name
            nodes.active = from_node
            named = True
            break
    if not named:
        # mat.name = "!3 No Texture"
        pass

def mat_apply_settings(mat, shader_type: str):
    """Setting material viewport settings."""
    mat.metallic = 0
    mat.roughness = 0.5
    mat.diffuse_color = (0.3, 0.3, 0.3, 1)
    # blend_method, show_transparent_back, use_screen_refraction, use_sss_translucency
    # were removed in Blender 4.2 (EEVEE Next). Only set them on older versions.
    _has_blend = hasattr(mat, 'blend_method')
    if shader_type == 'pbr_eye_shadow':
        if _has_blend:
            mat.blend_method = 'BLEND'
            mat.show_transparent_back = False
            mat.use_screen_refraction = True
            mat.use_sss_translucency = True
        set_shadow_method(mat)
    elif shader_type == 'pbr_eye':
        if _has_blend:
            mat.use_screen_refraction = True
    elif shader_type == 'transparent_lit':
        if _has_blend:
            mat.blend_method = 'BLEND'
            mat.show_transparent_back = False
            mat.use_screen_refraction = True
            mat.use_sss_translucency = True
        set_shadow_method(mat)
    else:
        if _has_blend:
            mat.blend_method = 'CLIP'




def create_texarray(group_name = "WitcherTexArray", ARRAY_SIZE = 2):
    vertex_color_data = []
    obj = bpy.context.active_object
    me = obj.data
    highest_green = 0
    if obj.type == "MESH":
        active_color = me.color_attributes.active_color
        for vert in me.vertices:
            if active_color:
                elem = active_color.data[vert.index]
                color = elem.color if hasattr(elem, 'color') else elem.vector
                vertex_color_data.append(list(color))
                highest_green = max(highest_green, color[1])

    # # Check if group already exists
    # if group_name in bpy.data.node_groups:
    #     group = bpy.data.node_groups[group_name]
    #     group.nodes.clear()
    #     group.inputs.clear()
    # else:
    #     # Create a new node group
    group = bpy.data.node_groups.new(group_name, 'ShaderNodeTree')
    
    output = group.nodes.new('NodeGroupOutput')
    output.location = (700, 0)
    
    
    if bpy.app.version >= (4, 0, 0):
        group.interface.new_socket(name="Output", in_out='OUTPUT', socket_type='NodeSocketColor')
    else:
        group.outputs.new('NodeSocketColor','Output')
    # Create a single input with two sockets
    input = group.nodes.new('NodeGroupInput')
    input.name = 'Array'
    input.location = (-400, 0)
    
    try:
        array_step = highest_green/ARRAY_SIZE
    except Exception as e:
        log.critical('ERROR CREATING TEXTURE ARRAY')
        return group


    for index in range(0,ARRAY_SIZE):
        this_index = index

        if bpy.app.version >= (4, 0, 0):
            group.interface.new_socket(name=f"Array_{str(this_index)}", in_out='INPUT', socket_type='NodeSocketColor')
        else:
            group.inputs.new('NodeSocketColor', f"Array_{str(this_index)}")


    #create the first mix
    mix = _new_mix_color_node(group)
    _mix_fac_input(mix).default_value = 0.5
    mix.location = (0, -100)

    privious_mix = mix

    if ARRAY_SIZE > 1:
        mix_a, mix_b = _mix_color_inputs(mix)
        group.links.new(input.outputs[0], mix_a)
        group.links.new(input.outputs[1], mix_b)
    # for i in range(ARRAY_SIZE):
    #     group.links.new(input.outputs[i], mix.inputs[i+1])

    group.links.new(_mix_color_output(mix), output.inputs[0])

    if bpy.app.version >= (4, 0, 0):
        color_attr = group.nodes.new('ShaderNodeAttribute')
        color_attr.attribute_type = 'GEOMETRY'
        color_attr.attribute_name = "Color"
    else:
        color_attr = group.nodes.new('ShaderNodeVertexColor')
        color_attr.layer_name = "Color"
    color_attr.location = (-300, 400)
    
    color_ramp = group.nodes.new('ShaderNodeValToRGB')
    #color_ramp.color_ramp.elements[1].position = array_step/1.2
    color_ramp.color_ramp.elements[1].position = array_step
    color_ramp.location = (200, 400)

    separate_color = group.nodes.new('ShaderNodeSeparateColor')
    separate_color.location = (0, 400)


    group.links.new(color_attr.outputs[0], separate_color.inputs[0])
    group.links.new(separate_color.outputs[1], color_ramp.inputs[0])
    group.links.new(color_ramp.outputs[0], _mix_fac_input(mix))

    for i in range(ARRAY_SIZE-2):
        i+=1
        color_ramp = group.nodes.new('ShaderNodeValToRGB')
        #color_ramp.color_ramp.elements[1].position = (array_step*(i+1))/1.2
        color_ramp.color_ramp.elements[1].position = array_step*(i+1)
        color_ramp.location = (-200, -400 * i)

        mix = _new_mix_color_node(group)
        _mix_fac_input(mix).default_value = 0.5
        mix.location = (200, -400 * i )
        mix_a, mix_b = _mix_color_inputs(mix)
        group.links.new(color_ramp.outputs[0], _mix_fac_input(mix))
        group.links.new(_mix_color_output(privious_mix), mix_a)
        group.links.new(input.outputs[i+1], mix_b)
        group.links.new(separate_color.outputs[1], color_ramp.inputs[0])
        group.links.new(_mix_color_output(mix), output.inputs[0])

        privious_mix = mix

    
    
    return group

def set_shadow_method(mat):
    render_engine = bpy.context.scene.render.engine

    if render_engine in ('BLENDER_EEVEE', 'BLENDER_EEVEE_NEXT'):
        if hasattr(mat, 'blend_method'):
            mat.blend_method = 'HASHED'
    elif render_engine == 'CYCLES':
        if hasattr(mat, 'use_transparent_shadow'):
            mat.use_transparent_shadow = True
