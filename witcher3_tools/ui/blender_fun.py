import logging
import struct

log = logging.getLogger(__name__)
from mathutils import Euler
from math import radians
from ..CR2W.witcher_cache.TextureCache.DDSUtils import DDSUtils
from ..CR2W.witcher_cache.TextureCache.DDS_Metadata import DDSMetadata
from ..CR2W.witcher_cache.TextureCache.DDS_Enums import EFormat
from ..CR2W.witcher_cache.TextureCache.TextureCacheItem import CommonImageTools

from ..CR2W.witcher_cache.TextureCache import LoadTextureManager
from ..CR2W.CR2W_types import getCR2W
from ..CR2W.CR2W_helpers import Enums
from ..CR2W import bStream
from .. import get_texture_path, get_uncook_path

COOKED_W2CUBE_RGBA8_HV_FLIP_ENABLED = False  # Debug compare vs uncooked raw payload
UNCOOKED_COMPRESSED_CUBEMAP_ASSUME_MIP_MAJOR = True  # Fix distorted BC cubemap raw-tail exports
_UNCOOKED_W2CUBE_ALPHA_BYTE_TO_VARIANT = {
    # alpha byte index -> (format_name, swizzle_to_rgba)
    0: ("ARGB", (1, 2, 3, 0)),
    3: ("RGBA", (0, 1, 2, 3)),
    1: ("BAGR", (3, 2, 0, 1)),
    2: ("RABG", (0, 3, 1, 2)),  # fallback for future/unknown variant
}
_UNCOOKED_W2CUBE_COMPRESSION_HINTS = (
    (b"TCM_DXTAlpha", EFormat.BC3_UNORM),        # DXT5 / BC3
    (b"TCM_DXTNoAlpha", EFormat.BC1_UNORM),      # DXT1 / BC1
    (b"TCM_None", EFormat.R8G8B8A8_UNORM),       # uncompressed RGBA8-ish
    (b"TCM_QualityColor", EFormat.BC7_UNORM),    # BC7
)


def reset_transforms(new_obj):
    x, y, z = (radians(0), radians(0), radians(0))
    mat = Euler((x, y, z)).to_matrix().to_4x4()
    new_obj.matrix_world = mat
    new_obj.matrix_local = mat
    new_obj.matrix_basis = mat

    new_obj.location[0] = 0
    new_obj.location[1] = 0
    new_obj.location[2] = 0
    new_obj.scale[0] = 1
    new_obj.scale[1] = 1
    new_obj.scale[2] = 1

class ImageUtility():
    @staticmethod
    def GetEFormatFromCompression(compression:str):
        if compression == Enums.ETextureCompression.TCM_None.name:
            return EFormat.R8G8B8A8_UNORM
        elif compression in (Enums.ETextureCompression.TCM_DXTNoAlpha.name, Enums.ETextureCompression.TCM_Normals.name):
            return EFormat.BC1_UNORM
        elif compression in (Enums.ETextureCompression.TCM_DXTAlpha.name, Enums.ETextureCompression.TCM_NormalsHigh.name, Enums.ETextureCompression.TCM_NormalsGloss.name):
            return EFormat.BC3_UNORM
        elif compression == Enums.ETextureCompression.TCM_QualityColor.name:
            return EFormat.BC7_UNORM
        elif compression == Enums.ETextureCompression.TCM_QualityR.name:
            return EFormat.BC4_UNORM
        elif compression == Enums.ETextureCompression.TCM_QualityRG.name:
            return EFormat.BC5_UNORM
        else:
            raise NotImplementedError("Compression type not implemented")

def GetDDSMetadata(chunk):
    residentMipIndex = chunk.GetVariableByName('residentMipIndex').Value if chunk.GetVariableByName('residentMipIndex') else 0
    mipcount = len(chunk.CBitmapTexture.Mipdata.bufferData) - residentMipIndex
    width = chunk.CBitmapTexture.Mipdata.bufferData[residentMipIndex].Width.val
    height = chunk.CBitmapTexture.Mipdata.bufferData[residentMipIndex].Height.val
    dxt = chunk.GetVariableByName('compression').Index.String if chunk.GetVariableByName('compression') else "TCM_None"
    textureCacheKey = chunk.GetVariableByName('textureCacheKey')
    
    ddsformat = ImageUtility.GetEFormatFromCompression(dxt)
    return DDSMetadata(width, height, mipcount, ddsformat)


def _swizzle_rgba8_bytes_to_bgra(raw_bytes: bytes) -> bytes:
    """Convert RGBA8 payload bytes to BGRA8 for legacy DDS A8R8G8B8 masks."""
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

def convert_xbm_to_dds(fdir, force=False, out_path=None):
    import os
    f = open(fdir,"rb")
    xbmFile = getCR2W(f)
    
    f.seek(0)
    br:bStream = bStream(data = f.read())
    f.close()
    
    ddsheader = b'\x44\x44\x53\x20\x7C\x00\x00\x00\x07\x10\x0A\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x20\x00\x00\x00\x05\x00\x00\x00\x44\x58\x54\x31\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x08\x10\x40\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'

    for chunk in xbmFile.CHUNKS.CHUNKS:
        if chunk.Type == "CBitmapTexture":
            width = chunk.GetVariableByName('width').Value
            height = chunk.GetVariableByName('height').Value
            
            if xbmFile.HEADER.version > 115:
                mipcount = 1
                residentMipIndex = chunk.GetVariableByName('residentMipIndex').Value if chunk.GetVariableByName('residentMipIndex') else 0
                mipcount = len(chunk.CBitmapTexture.Mipdata.bufferData) - residentMipIndex
                width = chunk.CBitmapTexture.Mipdata.bufferData[residentMipIndex].Width.val
                height = chunk.CBitmapTexture.Mipdata.bufferData[residentMipIndex].Height.val
                dxt = chunk.GetVariableByName('compression').Index.String if chunk.GetVariableByName('compression') else "TCM_None"
                textureCacheKey = chunk.GetVariableByName('textureCacheKey')
                #header = DDSMetadata(height=height, width=width, mipscount=mipcount, format=dxt)
                metadata = GetDDSMetadata(chunk)
                mipcount = struct.pack('i', mipcount)
            else:
                dxt = chunk.GetVariableByName('compression').Index.String if chunk.GetVariableByName('compression') else "TCM_None"
            width =  struct.pack('i',width)
            height = struct.pack('i',height)
            
            # TCM_None = 0
            # TCM_DXTNoAlpha = 1
            # TCM_DXTAlpha = 2
            # TCM_RGBE = 3  # unused
            # TCM_Normals = 4
            # TCM_NormalsHigh = 5
            # TCM_NormalsGloss = 6
            # TCM_DXTAlphaLinear = 7  # unused
            # TCM_QualityR = 8
            # TCM_QualityRG = 9
            # TCM_QualityColor = 10
            
            # b'x\x31\x54\x58\x44':    //DXT1
            #     Format = EFormat.BC1_UNORM;
            #     break;
            # case 0x33545844:    //DXT3
            #     Format = EFormat.BC2_UNORM;
            #     break;
            # case 0x35545844:    //DXT5
            #     Format = EFormat.BC3_UNORM;
            #     break;
            # case 0x55344342:    //BC4U
            #     Format = EFormat.BC4_UNORM;
            #     break;
            # case 0x55354342:    //BC5U
            #     Format = EFormat.BC5_UNORM;
            #     break;
            
            if dxt in ['TCM_DXTNoAlpha', 'TCM_Normals']:
                dxt = b'\x44\x58\x54\x31'#'DXT1'
            elif dxt == 'TCM_None':
                dxt = b'\x00\x00\x00\x00'
            elif dxt in ['TCM_DXTAlpha', 'TCM_NormalsHigh', 'TCM_NormalsGloss']:
                dxt = b'\x44\x58\x54\x35'
            elif dxt in ['TCM_QualityColor']:
                dxt = b'\x44\x58\x54\x35'
            else:
                raise('Unknown dxt')
            dds_path = os.path.splitext(out_path or fdir)[0] + '.dds'
            if os.path.exists(dds_path) and not force:
                return dds_path
            br.seek(chunk.PROPS[-1].dataEnd)
            
            if xbmFile.HEADER.version <= 115:
                br.seek(27, 1)
                new = open(dds_path,'wb')
                new.write(ddsheader)
                new.seek(0xC)
                new.write(height)
                new.seek(0x10)
                new.write(width)
                new.seek(0x54)
                new.write(dxt)
                new.seek(128)
                new.write(br.read(None))
                new.close()
            else:
                export_from_texture_cache = True
                
                is_cooked = any(export.objectFlags == 8192 and export.name == 'CBitmapTexture' for export in xbmFile.CR2WExport)
                
                if is_cooked and export_from_texture_cache:
                    texture_item = None
                    # Try vanilla first (common case), then fall back to mods
                    for use_mods in (False, True):
                        texture_manager = LoadTextureManager(loadmods=use_mods)
                        item = texture_manager.find_item_by_hash(textureCacheKey.Value)
                        if item:
                            texture_item = item[-1]  # last bundle loaded, should be top mod
                            break
                    if texture_item:
                        import os
                        import bpy
                        extractPath = out_path or os.path.join(get_texture_path(bpy.context), texture_item.Name)
                        texture_item.extract_to_file(extractPath)
                        dds_path = os.path.splitext(extractPath)[0] + '.dds'
                    else:
                        log.debug("Uncooked Texture not found in cache, using cooked file. %s", fdir)
                        #!REMOVE REPEATED CODE CLEAN UP
                        # new = open(dds_path,'wb')
                        # new.write(ddsheader)
                        # new.seek(0xC) #12
                        # new.write(height)
                        # new.seek(0x10) # 16
                        # new.write(width)
                        # new.seek(0x1C) # 28
                        # new.write(mipcount)
                        # new.seek(0x54) #84
                        # new.write(dxt)
                        # new.seek(128)
                        output_stream:bStream = bStream(path = dds_path)
                        output_stream.decoder = 'ISO-8859-1'
                        DDSUtils.GenerateAndWriteHeader(output_stream, metadata)

                        if is_cooked:
                            payload = chunk.CBitmapTexture.Residentmip.val
                        else:
                            if len(chunk.CBitmapTexture.Mipdata.bufferData) <= 0:
                                return None
                            payload = b''.join(buff.Mip.Bytes for buff in chunk.CBitmapTexture.Mipdata.bufferData)

                        if metadata.format == EFormat.R8G8B8A8_UNORM:
                            payload = _swizzle_rgba8_bytes_to_bgra(payload)
                        output_stream.write(payload)

                        output_stream.close()
                    #load texture cache object
                    #use object to lookup key
                    #write the dds
                    # texture_cache:TextureCache = TextureCache(get_game_path())
                    # texture_cache.get_texture(textureCacheKey.Value)
                    # print(textureCacheKey.Value)
                else:
                    new = open(dds_path,'wb')
                    new.write(ddsheader)
                    new.seek(0xC) #12
                    new.write(height)
                    new.seek(0x10) # 16
                    new.write(width)
                    new.seek(0x1C) # 28
                    new.write(mipcount)
                    new.seek(0x54) #84
                    new.write(dxt)
                    new.seek(128)

                    if is_cooked:
                        payload = chunk.CBitmapTexture.Residentmip.val
                    else:
                        if len(chunk.CBitmapTexture.Mipdata.bufferData) <= 0:
                            return None
                        payload = b''.join(buff.Mip.Bytes for buff in chunk.CBitmapTexture.Mipdata.bufferData)

                    if metadata.format == EFormat.R8G8B8A8_UNORM:
                        payload = _swizzle_rgba8_bytes_to_bgra(payload)
                    new.write(payload)

                    new.close()
                
                

            break
    return dds_path


def _dds_format_name_from_header(data: bytes):
    if len(data) < 128 or data[:4] != b"DDS ":
        raise ValueError("Invalid DDS file")

    header_size = struct.unpack_from("<I", data, 4)[0]
    if header_size != 124:
        raise ValueError("Invalid DDS header size")

    height = struct.unpack_from("<I", data, 12)[0]
    width = struct.unpack_from("<I", data, 16)[0]
    mip_count = struct.unpack_from("<I", data, 28)[0]
    pf_size = struct.unpack_from("<I", data, 76)[0]
    fourcc = data[84:88]
    rgb_bit_count = struct.unpack_from("<I", data, 88)[0]
    rmask = struct.unpack_from("<I", data, 92)[0]
    gmask = struct.unpack_from("<I", data, 96)[0]
    bmask = struct.unpack_from("<I", data, 100)[0]
    amask = struct.unpack_from("<I", data, 104)[0]

    if pf_size != 32:
        raise ValueError("Invalid DDS pixel format size")
    if width <= 0 or height <= 0:
        raise ValueError("Invalid DDS dimensions")

    if fourcc == b"DX10":
        if len(data) < 148:
            raise ValueError("Invalid DX10 DDS header")
        dxgi_format = struct.unpack_from("<I", data, 128)[0]
        if dxgi_format in (71, 72):
            format_name = "BC1_UNORM"
        elif dxgi_format in (74, 75):
            format_name = "BC2_UNORM"
        elif dxgi_format in (77, 78):
            format_name = "BC3_UNORM"
        elif dxgi_format == 80:
            format_name = "BC4_UNORM"
        elif dxgi_format == 83:
            format_name = "BC5_UNORM"
        elif dxgi_format in (28, 29, 87, 91):
            format_name = "R8G8B8A8_UNORM"
        elif dxgi_format in (98, 99):
            format_name = "BC7_UNORM"
        else:
            raise ValueError(f"Unsupported DXGI format in DDS: {dxgi_format}")
        data_offset = 148
    elif fourcc == b"DXT1":
        format_name = "BC1_UNORM"
        data_offset = 128
    elif fourcc == b"DXT3":
        format_name = "BC2_UNORM"
        data_offset = 128
    elif fourcc == b"DXT5":
        format_name = "BC3_UNORM"
        data_offset = 128
    elif fourcc == b"BC4U":
        format_name = "BC4_UNORM"
        data_offset = 128
    elif fourcc == b"BC5U":
        format_name = "BC5_UNORM"
        data_offset = 128
    elif fourcc == b"\x00\x00\x00\x00" and rgb_bit_count == 32:
        if (rmask, gmask, bmask, amask) in (
            (0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000),
            (0x000000FF, 0x0000FF00, 0x00FF0000, 0xFF000000),
        ):
            format_name = "R8G8B8A8_UNORM"
            data_offset = 128
        else:
            raise ValueError("Unsupported uncompressed DDS pixel masks")
    else:
        raise ValueError(f"Unsupported DDS format: fourcc={fourcc!r}")

    return format_name, width, height, max(1, mip_count), data_offset


def _dds_top_mip_size(format_name: str, width: int, height: int) -> int:
    width = int(width)
    height = int(height)
    if format_name == "R8G8B8A8_UNORM":
        return width * height * 4

    block_width = max(1, (width + 3) // 4)
    block_height = max(1, (height + 3) // 4)
    if format_name in ("BC1_UNORM", "BC4_UNORM"):
        return block_width * block_height * 8
    if format_name in ("BC2_UNORM", "BC3_UNORM", "BC5_UNORM", "BC7_UNORM"):
        return block_width * block_height * 16
    raise ValueError(f"Unsupported DDS format: {format_name}")


def is_valid_dds_file(dds_path: str) -> bool:
    import os

    try:
        if not dds_path or not os.path.exists(dds_path):
            return False
        with open(dds_path, "rb") as handle:
            header_data = handle.read(148)
        format_name, width, height, _mip_count, data_offset = _dds_format_name_from_header(header_data)
        file_size = os.path.getsize(dds_path)
        min_payload = _dds_top_mip_size(format_name, width, height)
        return file_size >= (data_offset + min_payload)
    except Exception:
        return False


def _blender_image_has_data(image) -> bool:
    if image is None:
        return False
    try:
        size = tuple(getattr(image, "size", (0, 0)))
    except Exception:
        return False
    if len(size) < 2 or size[0] <= 0 or size[1] <= 0:
        return False
    has_data = getattr(image, "has_data", None)
    if has_data is not None and not has_data:
        return False
    return True


def _normalize_texture_repo_path(repo_path: str) -> str:
    import os

    normalized = str(repo_path or "").replace("/", "\\").lstrip("\\")
    if not normalized:
        return ""

    base, ext = os.path.splitext(normalized)
    if ext.lower() in {".dds", ".png", ".jpg", ".jpeg", ".tga", ".bmp"}:
        return base + ".xbm"
    return normalized


def _resolve_texture_repo_path_for_repair(image_path: str, image=None) -> str:
    repo_path = ""

    if image is not None:
        try:
            settings = getattr(image, "witcherui_TextureSettings", None)
            repo_path = getattr(settings, "repo_path", "") or ""
        except Exception:
            repo_path = ""

    repo_path = _normalize_texture_repo_path(repo_path)
    if repo_path:
        return repo_path

    try:
        from .ui_texture_export import resolve_texture_image_metadata

        repo_path, _texture_group = resolve_texture_image_metadata(
            bpy.context,
            image_path,
            repo_path=repo_path,
        )
    except Exception:
        repo_path = ""

    return _normalize_texture_repo_path(repo_path)


def _refresh_local_xbm_for_repair(image_path: str, image=None):
    import os
    from ..CR2W.common_blender import repo_file, win_safe_path, win_unprefix_path

    sibling_xbm_path = os.path.splitext(image_path)[0] + ".xbm"
    repo_path = _resolve_texture_repo_path_for_repair(image_path, image=image)
    last_error = None

    if repo_path:
        try:
            refreshed_xbm_path = win_unprefix_path(repo_file(repo_path))
            if refreshed_xbm_path and os.path.isfile(win_safe_path(refreshed_xbm_path)):
                return refreshed_xbm_path, None
        except Exception as exc:
            last_error = exc

    if os.path.isfile(win_safe_path(sibling_xbm_path)):
        return sibling_xbm_path, last_error

    return "", last_error


def _repair_dds_from_local_xbm(image_path: str, xbm_path: str = ""):
    import os
    from ..CR2W.common_blender import win_safe_path, win_unprefix_path

    xbm_path = win_unprefix_path(xbm_path or "")
    if not xbm_path:
        xbm_path = os.path.splitext(image_path)[0] + ".xbm"
    if not os.path.isfile(win_safe_path(xbm_path)):
        return False, None

    try:
        convert_xbm_to_dds(xbm_path, force=True, out_path=image_path)
    except Exception as exc:
        return False, exc

    return is_valid_dds_file(image_path), None


def load_image_with_dds_repair(image_path: str, *, image=None, check_existing=True, allow_dds_repair=False):
    import os
    from ..CR2W.common_blender import bpy_image_load_safe, win_unprefix_path

    image_path = win_unprefix_path(image_path or "")
    is_dds = image_path.lower().endswith(".dds")
    last_error = None
    source_image = image

    def _repair_dds() -> bool:
        nonlocal last_error
        if not (allow_dds_repair and is_dds):
            return False
        refreshed_xbm_path, refresh_error = _refresh_local_xbm_for_repair(image_path, image=source_image)
        if refresh_error is not None:
            last_error = refresh_error

        if refreshed_xbm_path:
            try:
                repaired, error = _repair_dds_from_local_xbm(image_path, xbm_path=refreshed_xbm_path)
            except Exception as exc:
                repaired, error = False, exc
            if repaired:
                return True
            if error is not None:
                last_error = error
        return False

    if is_dds and not is_valid_dds_file(image_path):
        _repair_dds()

    for attempt in range(2):
        loaded_image = None
        try:
            loaded_image = bpy_image_load_safe(image_path, check_existing=check_existing)
            try:
                loaded_image.reload()
            except Exception as exc:
                last_error = exc
            if _blender_image_has_data(loaded_image):
                return loaded_image, None
            if last_error is None:
                last_error = RuntimeError(
                    f"Blender failed to decode image data for {os.path.basename(image_path)}"
                )
        except Exception as exc:
            last_error = exc

        if attempt == 0 and _repair_dds():
            continue
        break

    return None, last_error


def _flip_rgba8_cubemap_faces_hv(raw_bytes: bytes, edge: int, mip_count: int) -> bytes:
    """Prepare RGBA8 cubemap DDS bytes (optional H+V face flip + BGRA swizzle).

    Assumes face-major layout and source RGBA8 texels (4 bytes/pixel).
    Output bytes are swizzled to BGRA to match the legacy DDS header masks
    generated for `R8G8B8A8_UNORM` elsewhere in the toolchain.
    """
    try:
        edge = int(edge or 0)
        mip_count = int(mip_count or 0)
    except Exception:
        return raw_bytes
    if edge <= 0 or mip_count <= 0:
        return raw_bytes

    src = memoryview(raw_bytes)
    out = bytearray()
    cursor = 0

    for _face_idx in range(6):
        w = edge
        h = edge
        for _mip_idx in range(mip_count):
            pixel_count = max(1, w) * max(1, h)
            byte_count = pixel_count * 4
            chunk = src[cursor:cursor + byte_count]
            if len(chunk) != byte_count:
                return raw_bytes

            # Optional H+V flip (reverse pixel order) + RGBA -> BGRA swizzle so
            # bytes match the DDS header masks (A8R8G8B8 style legacy header).
            for dst_idx in range(pixel_count):
                if COOKED_W2CUBE_RGBA8_HV_FLIP_ENABLED:
                    src_idx = pixel_count - 1 - dst_idx
                else:
                    src_idx = dst_idx
                off = src_idx * 4
                out.extend((chunk[off + 2], chunk[off + 1], chunk[off + 0], chunk[off + 3]))

            cursor += byte_count
            w = max(1, w // 2)
            h = max(1, h // 2)

    if cursor != len(raw_bytes):
        return raw_bytes
    return bytes(out)


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


def _uncooked_w2cube_tail_face_size_for_eformat(edge: int, eformat: EFormat):
    if eformat == EFormat.R8G8B8A8_UNORM:
        return _rgba8_full_mip_chain_face_size(edge)
    if eformat == EFormat.BC1_UNORM:
        return _block_compressed_full_mip_chain_face_size(edge, 8)
    if eformat in (EFormat.BC2_UNORM, EFormat.BC3_UNORM, EFormat.BC5_UNORM, EFormat.BC7_UNORM):
        return _block_compressed_full_mip_chain_face_size(edge, 16)
    return None


def _block_bytes_for_eformat(eformat: EFormat):
    if eformat == EFormat.BC1_UNORM:
        return 8
    if eformat in (EFormat.BC2_UNORM, EFormat.BC3_UNORM, EFormat.BC5_UNORM, EFormat.BC7_UNORM):
        return 16
    return None


def _repack_block_cubemap_mip_major_to_face_major(raw_bytes: bytes, edge: int, mip_count: int, block_bytes: int) -> bytes:
    """Repack a BC-compressed cubemap payload from mip-major to DDS face-major layout."""
    try:
        edge = int(edge or 0)
        mip_count = int(mip_count or 0)
        block_bytes = int(block_bytes or 0)
    except Exception:
        return raw_bytes
    if edge <= 0 or mip_count <= 0 or block_bytes not in (8, 16):
        return raw_bytes

    src = memoryview(raw_bytes)
    cursor = 0
    face_chunks = [bytearray() for _ in range(6)]
    w = edge
    h = edge
    for _mip_idx in range(mip_count):
        bw = max(1, (w + 3) // 4)
        bh = max(1, (h + 3) // 4)
        mip_face_size = bw * bh * block_bytes
        for face_idx in range(6):
            chunk = src[cursor:cursor + mip_face_size]
            if len(chunk) != mip_face_size:
                return raw_bytes
            face_chunks[face_idx].extend(chunk)
            cursor += mip_face_size
        w = max(1, w // 2)
        h = max(1, h // 2)
    if cursor != len(raw_bytes):
        return raw_bytes
    out = bytearray(len(raw_bytes))
    off = 0
    for face_idx in range(6):
        chunk = face_chunks[face_idx]
        out[off:off + len(chunk)] = chunk
        off += len(chunk)
    return bytes(out)


def _detect_uncooked_w2cube_compression_hint(header_bytes: bytes):
    if not header_bytes:
        return None, None
    for marker, eformat in _UNCOOKED_W2CUBE_COMPRESSION_HINTS:
        if marker in header_bytes:
            return marker.decode("ascii", errors="ignore"), eformat
    return None, None


def _find_uncooked_w2cube_tail_payload_layout(file_bytes: bytes):
    """Detect raw 6-face full-mip cubemap payload packed at end of an uncooked .w2cube.

    Returns a dict with:
      edge, mip_count, payload_start, face_full_size, payload_size, eformat,
      compression_hint, header_size
    """
    if not file_bytes:
        return None
    file_size = len(file_bytes)
    header_preview = file_bytes[:min(file_size, 4096)]
    compression_hint_name, hinted_eformat = _detect_uncooked_w2cube_compression_hint(header_preview)

    formats_to_try = [EFormat.R8G8B8A8_UNORM, EFormat.BC1_UNORM, EFormat.BC3_UNORM, EFormat.BC7_UNORM]
    if hinted_eformat in formats_to_try:
        formats_to_try = [hinted_eformat] + [f for f in formats_to_try if f != hinted_eformat]

    candidates = []
    for eformat in formats_to_try:
        for exp in range(3, 14):  # 8 .. 8192
            edge = 1 << exp
            face_full_size = _uncooked_w2cube_tail_face_size_for_eformat(edge, eformat)
            if not face_full_size:
                continue
            payload_size = face_full_size * 6
            if payload_size <= 0 or payload_size > file_size:
                continue
            header_size = file_size - payload_size
            if not (16 <= header_size <= 65536):
                continue
            if header_size < 256:
                continue
            header = file_bytes[:min(header_size, 4096)]
            has_uncooked_markers = (
                (b"CCubeTexture" in header) and
                ((b"CBitmapTexture" in header) or (b"CubeFace" in header))
            )
            if not has_uncooked_markers:
                continue
            candidates.append({
                "edge": edge,
                "mip_count": exp + 1,
                "payload_start": file_size - payload_size,
                "face_full_size": face_full_size,
                "payload_size": payload_size,
                "eformat": eformat,
                "compression_hint": compression_hint_name,
                "header_size": header_size,
                "hint_match": bool(hinted_eformat is not None and eformat == hinted_eformat),
            })
    if not candidates:
        return None
    # Selection heuristics:
    # 1) Prefer compression-hint matches (TCM_* strings in header)
    # 2) Otherwise prefer uncompressed RGBA8 to preserve existing working cases
    #    like forest_cube where no compression string is present.
    # 3) Then prefer larger edge.
    def _rank(c):
        return (
            1 if c.get("hint_match") else 0,
            1 if c.get("eformat") == EFormat.R8G8B8A8_UNORM else 0,
            int(c.get("edge") or 0),
        )
    return max(candidates, key=_rank)


def _count_ff_per_channel_rgba8(data: bytes, offset: int, sample_pixels: int = 512):
    counts = [0, 0, 0, 0]
    if not data or offset < 0 or offset >= len(data):
        return counts
    limit = min(int(sample_pixels or 0), max(0, (len(data) - offset) // 4))
    for i in range(limit):
        base = offset + i * 4
        for c in range(4):
            if data[base + c] == 0xFF:
                counts[c] += 1
    return counts


def _detect_uncooked_w2cube_tail_pixel_variant(payload_bytes: bytes):
    """Detect uncooked raw-tail pixel byte order by alpha-channel position."""
    counts = _count_ff_per_channel_rgba8(payload_bytes, 0, sample_pixels=512)
    alpha_byte = counts.index(max(counts)) if counts else 3
    fmt_name, swizzle_rgba = _UNCOOKED_W2CUBE_ALPHA_BYTE_TO_VARIANT.get(
        alpha_byte,
        (f"UNKNOWN_alpha{alpha_byte}", (0, 1, 2, 3)),
    )
    # DDSUtils writes legacy R8G8B8A8_UNORM as A8R8G8B8-style masks, so write
    # bytes in BGRA order. Compose RGBA swizzle -> BGRA source indices.
    swizzle_bgra = (swizzle_rgba[2], swizzle_rgba[1], swizzle_rgba[0], swizzle_rgba[3])
    return fmt_name, alpha_byte, counts, swizzle_rgba, swizzle_bgra


def _swizzle_uncooked_w2cube_tail_to_bgra(raw_bytes: bytes, swizzle_bgra) -> bytes:
    """Swizzle uncooked raw-tail cubemap bytes to BGRA for DDS export."""
    if not raw_bytes:
        return raw_bytes
    if not swizzle_bgra or tuple(swizzle_bgra) == (0, 1, 2, 3):
        return raw_bytes
    s0, s1, s2, s3 = swizzle_bgra
    src = memoryview(raw_bytes)
    out = bytearray(len(raw_bytes))
    for i in range(0, len(raw_bytes), 4):
        out[i + 0] = src[i + s0]
        out[i + 1] = src[i + s1]
        out[i + 2] = src[i + s2]
        out[i + 3] = src[i + s3]
    return bytes(out)


def _write_uncooked_w2cube_raw_tail_payload_to_dds(fdir: str, file_bytes: bytes):
    """Export an uncooked/source .w2cube raw tail payload to a cubemap DDS."""
    layout = _find_uncooked_w2cube_tail_payload_layout(file_bytes)
    if not layout:
        return None
    edge = int(layout["edge"])
    mip_count = int(layout["mip_count"])
    payload_start = int(layout["payload_start"])
    face_full_size = int(layout["face_full_size"])
    eformat = layout.get("eformat") or EFormat.R8G8B8A8_UNORM
    payload = file_bytes[payload_start:]
    expected = face_full_size * 6
    if len(payload) != expected:
        return None

    if eformat == EFormat.R8G8B8A8_UNORM:
        fmt_name, alpha_byte, alpha_counts, _swizzle_rgba, swizzle_bgra = _detect_uncooked_w2cube_tail_pixel_variant(payload)
        raw_bytes = _swizzle_uncooked_w2cube_tail_to_bgra(payload, swizzle_bgra)
        layout_note = "face-major(raw RGBA tail)"
    else:
        fmt_name = getattr(eformat, "name", str(eformat))
        alpha_byte = None
        alpha_counts = None
        raw_bytes = payload
        layout_note = "unknown"
        block_bytes = _block_bytes_for_eformat(eformat)
        if block_bytes and UNCOOKED_COMPRESSED_CUBEMAP_ASSUME_MIP_MAJOR:
            repacked = _repack_block_cubemap_mip_major_to_face_major(raw_bytes, edge, mip_count, block_bytes)
            if repacked is not raw_bytes:
                raw_bytes = repacked
                layout_note = "mip-major->face-major (BC)"
            else:
                layout_note = "face-major? (BC repack noop)"
        elif block_bytes:
            layout_note = "face-major (BC)"

    dds_path = fdir.replace('.w2cube', '_cubemap.dds')
    output_stream: bStream = bStream(path=dds_path)
    output_stream.decoder = 'ISO-8859-1'
    metadata = DDSMetadata(
        width=edge,
        height=edge,
        mipscount=mip_count,
        format=eformat,
        iscubemap=True,
        slicecount=6,
        normal=False
    )
    DDSUtils.GenerateAndWriteHeader(output_stream, metadata)
    output_stream.write(raw_bytes)
    output_stream.close()

    log.info(
        "convert_w2cube_to_dds: exported uncooked raw cubemap tail payload to DDS (%s, edge=%s, mips=%s, payload_start=%s, eformat=%s, variant=%s, alpha_byte=%s, alpha_counts=%s, hint=%s, layout=%s)",
        dds_path, edge, mip_count, payload_start, getattr(eformat, 'name', eformat), fmt_name, alpha_byte, alpha_counts, layout.get("compression_hint"), layout_note
    )
    return dds_path


def convert_w2cube_to_dds(fdir):
    """Parse a cooked .w2cube file and write a cubemap DDS.

    Cooked CCubeTexture buffer layout (after CR2W chunk properties):
      uint32  texturecachekey
      uint16  residentmip     (mip levels 0..residentmip-1 are in TextureCache, not here)
      uint16  encodedformat   (low byte = redengine format byte)
      uint16  edge            (face width/height in pixels at mip 0)
      uint16  mipmapscount    (total mip count including non-resident)
      uint32  filesize        (byte count of raw image data following)
      int32   ffffffff        (sentinel = -1)
      bytes   rawfile[filesize]

    For full resolution, the TextureCache is queried first using texturecachekey.
    If not found, the embedded low-res data is used (starting at residentmip).
    """
    import os as _os

    f = open(fdir, "rb")
    file_bytes = f.read()
    f.close()
    file_size = len(file_bytes)

    # Uncooked/source cubemaps can store a raw full cubemap payload at the end
    # of the file. Export them directly without invoking the CR2W parser so the
    # import path matches cooked assets (DDS -> face previews).
    uncooked_dds = _write_uncooked_w2cube_raw_tail_payload_to_dds(fdir, file_bytes)
    if uncooked_dds:
        return uncooked_dds

    f = open(fdir, "rb")
    w2cubeFile = getCR2W(f)
    f.close()
    br: bStream = bStream(data=file_bytes)

    for chunk in w2cubeFile.CHUNKS.CHUNKS:
        if chunk.Type == "CCubeTexture":
            # Seek to where the inline buffer data begins
            buffer_start = getattr(chunk, 'cube_buffer_start', None)
            if buffer_start is None:
                if chunk.PROPS:
                    buffer_start = chunk.PROPS[-1].dataEnd
                else:
                    continue
            br.seek(buffer_start)

            texturecachekey = br.readUInt32()
            residentmip     = br.readUInt16()
            encodedformat   = br.readUInt16()
            edge            = br.readUInt16()
            mipmapscount    = br.readUInt16()
            filesize        = br.readUInt32()
            ffffffff        = br.readInt32()  # sentinel -1

            dds_path = fdir.replace('.w2cube', '_cubemap.dds')
            if _os.path.exists(dds_path):
                return dds_path

            # Uncooked/source cubemaps can have CCubeTexture face metadata but no
            # cooked runtime payload at `buffer_start`. Validate the header before
            # attempting a texture-cache lookup (which can be very slow if the
            # parsed key is just garbage bytes).
            data_pos = br.tell()
            remaining = max(0, file_size - data_pos)
            edge_is_pow2 = edge > 0 and (edge & (edge - 1)) == 0
            header_plausible = (
                edge_is_pow2 and
                1 <= edge <= 16384 and
                1 <= mipmapscount <= 32 and
                0 <= residentmip <= mipmapscount and
                0 <= filesize <= remaining and
                ffffffff == -1
            )
            if not header_plausible:
                raise ValueError(
                    "CCubeTexture does not contain a cooked runtime cubemap payload "
                    f"(likely uncooked/source .w2cube). Parsed header: key={texturecachekey}, "
                    f"residentmip={residentmip}, format=0x{encodedformat & 0xFFFF:04X}, "
                    f"edge={edge}, mips={mipmapscount}, filesize={filesize}, sentinel={ffffffff}"
                )

            # --- Strategy 1: Full resolution from TextureCache ---
            if texturecachekey:
                for use_mods in (False, True):
                    try:
                        texture_manager = LoadTextureManager(loadmods=use_mods)
                        items = texture_manager.find_item_by_hash(texturecachekey)
                        if items:
                            texture_item = items[-1]
                            texture_item.extract_to_file(dds_path)
                            if _os.path.exists(dds_path):
                                return dds_path
                    except Exception:
                        pass

            # --- Strategy 2: Embedded low-res data (residentmip onwards) ---
            raw_bytes = br.read(filesize)
            format_byte = encodedformat & 0xFF
            eformat = CommonImageTools.get_eformat_from_redengine_byte(format_byte)

            # Actual resolution stored is edge >> residentmip
            actual_edge = edge >> residentmip if residentmip > 0 else edge
            actual_mips = mipmapscount - residentmip
            if eformat == EFormat.R8G8B8A8_UNORM:
                raw_bytes = _flip_rgba8_cubemap_faces_hv(raw_bytes, actual_edge, actual_mips)

            from ..CR2W.witcher_cache.TextureCache.DDS_Metadata import DDSMetadata
            metadata = DDSMetadata(
                width=actual_edge,
                height=actual_edge,
                mipscount=actual_mips,
                format=eformat,
                iscubemap=True,
                slicecount=6,
                normal=False
            )

            output_stream: bStream = bStream(path=dds_path)
            output_stream.decoder = 'ISO-8859-1'
            DDSUtils.GenerateAndWriteHeader(output_stream, metadata)
            output_stream.write(raw_bytes)
            output_stream.close()

            return dds_path

    return None


def load_w2cube_image(fdir, *, check_existing=True, colorspace='sRGB'):
    """Convert a .w2cube to DDS and load it as a Blender image.

    Returns `(image, dds_path)`. `image` can be `None` if conversion/load failed.
    """
    import os as _os
    from ..CR2W.common_blender import bpy_image_load_safe

    dds_path = convert_w2cube_to_dds(fdir)
    if not dds_path or not _os.path.exists(dds_path):
        return None, dds_path

    img = bpy_image_load_safe(dds_path, check_existing=check_existing)
    if img and colorspace:
        try:
            img.reload()
        except Exception:
            pass
        try:
            img.colorspace_settings.name = colorspace
        except Exception:
            pass
    return img, dds_path


_BLICK_EQ_FACE_W2_SUFFIX = {
    "front": "fr",
    "back": "bk",
    "left": "lf",
    "right": "rt",
    "up": "up",
    "down": "dn",
}
_BLICK_EQ_CUBEMAP_FACE_TO_NAME = {
    "PY": "front",   # +Y
    "NY": "back",    # -Y
    "NX": "left",    # -X
    "PX": "right",   # +X
    "PZ": "up",      # +Z
    "NZ": "down",    # -Z
}
_BLICK_EQ_FACE_ROTATIONS = {
    "front": (180.0, 0.0, 0.0),
    "back": (0.0, 180.0, 0.0),
    "up": (180.0, 0.0, 0.0),
    "down": (180.0, 0.0, 180.0),
    "right": (0.0, 180.0, 90.0),
    "left": (180.0, 0.0, 90.0),
}


def _blick_apply_face_rotation_np(pixels, rotation):
    """Apply REDengine -> Blender face-orientation correction to a cubemap face array."""
    x_rot, y_rot, z_rot = rotation

    if int(round(float(x_rot))) % 360 == 180:
        pixels = pixels[::-1, :, :]
    if int(round(float(y_rot))) % 360 == 180:
        pixels = pixels[:, ::-1, :]
    if z_rot:
        import numpy as np

        k = int(round(float(z_rot) / 90.0)) % 4
        if k:
            pixels = np.rot90(pixels, k=k)
    return pixels


def _blick_load_face_images_from_dds_files(face_files, *, check_existing=True):
    """Load exported cubemap face DDS files into `{front/back/left/right/up/down: np.ndarray}`."""
    import numpy as np
    from ..CR2W.common_blender import bpy_image_load_safe

    faces = {}
    for cube_face_key, face_name in _BLICK_EQ_CUBEMAP_FACE_TO_NAME.items():
        face_path = face_files.get(cube_face_key)
        if not face_path:
            continue

        img = bpy_image_load_safe(face_path, check_existing=check_existing)
        if not img:
            continue
        try:
            img.reload()
        except Exception:
            pass

        w, h = img.size
        if not w or not h:
            continue

        pixels = np.zeros(w * h * 4, dtype=np.float32)
        img.pixels.foreach_get(pixels)
        pixels = pixels.reshape(h, w, 4)

        # Blender stores image pixels bottom-to-top.
        pixels = np.flipud(pixels)

        rot = _BLICK_EQ_FACE_ROTATIONS.get(face_name, (0.0, 0.0, 0.0))
        if rot != (0.0, 0.0, 0.0):
            pixels = _blick_apply_face_rotation_np(pixels, rot)

        faces[face_name] = pixels

    return faces


def _blick_cubemap_to_equirectangular_np(faces, out_width=None, out_height=None):
    """Convert six corrected cubemap face arrays to an equirectangular RGBA image."""
    import numpy as np
    from math import pi

    sample_face = next(iter(faces.values()))
    face_h, face_w = sample_face.shape[:2]

    if out_width is None:
        out_width = face_w * 4
    if out_height is None:
        out_height = out_width // 2

    equirect = np.zeros((out_height, out_width, 4), dtype=np.float32)

    u = np.linspace(0.5 / out_width, 1.0 - 0.5 / out_width, out_width, dtype=np.float32)
    v = np.linspace(0.5 / out_height, 1.0 - 0.5 / out_height, out_height, dtype=np.float32)
    uu, vv = np.meshgrid(u, v)

    theta = (uu - 0.5) * (2.0 * pi)
    phi = (0.5 - vv) * pi

    x = np.cos(phi) * np.sin(theta)
    y = np.sin(phi)
    z = np.cos(phi) * np.cos(theta)

    abs_x = np.abs(x)
    abs_y = np.abs(y)
    abs_z = np.abs(z)

    eps = 1e-10
    face_defs = {
        "right": ((x > 0) & (abs_x >= abs_y) & (abs_x >= abs_z),
                  -z / np.where(x != 0, x, eps),
                  -y / np.where(x != 0, x, eps)),
        "left": ((x < 0) & (abs_x >= abs_y) & (abs_x >= abs_z),
                 z / np.where(abs_x != 0, abs_x, eps),
                 -y / np.where(abs_x != 0, abs_x, eps)),
        "up": ((y > 0) & (abs_y >= abs_x) & (abs_y >= abs_z),
               x / np.where(y != 0, y, eps),
               z / np.where(y != 0, y, eps)),
        "down": ((y < 0) & (abs_y >= abs_x) & (abs_y >= abs_z),
                 x / np.where(abs_y != 0, abs_y, eps),
                 -z / np.where(abs_y != 0, abs_y, eps)),
        "front": ((z > 0) & (abs_z >= abs_x) & (abs_z >= abs_y),
                  x / np.where(z != 0, z, eps),
                  -y / np.where(z != 0, z, eps)),
        "back": ((z < 0) & (abs_z >= abs_x) & (abs_z >= abs_y),
                 -x / np.where(abs_z != 0, abs_z, eps),
                 -y / np.where(abs_z != 0, abs_z, eps)),
    }

    for face_name, (mask, uc, vc) in face_defs.items():
        face_data = faces.get(face_name)
        if face_data is None:
            continue

        rows, cols = np.where(mask)
        if rows.size == 0:
            continue

        px = np.clip(((uc[mask] + 1.0) * 0.5 * (face_w - 1)).astype(np.int32), 0, face_w - 1)
        py = np.clip(((vc[mask] + 1.0) * 0.5 * (face_h - 1)).astype(np.int32), 0, face_h - 1)
        equirect[rows, cols] = face_data[py, px]

    return equirect


def _blick_store_equirect_image(equirect_data, image_name: str, *, colorspace='sRGB'):
    """Create/update a packed Blender image from an equirectangular numpy RGBA array."""
    import bpy
    import numpy as np

    h, w = equirect_data.shape[:2]
    img = bpy.data.images.get(image_name)
    if img is None:
        img = bpy.data.images.new(
            image_name,
            width=int(w),
            height=int(h),
            alpha=True,
            float_buffer=True,
        )
    else:
        if tuple(img.size) != (int(w), int(h)):
            try:
                img.scale(int(w), int(h))
            except Exception:
                pass

    flipped = np.flipud(equirect_data)
    img.pixels.foreach_set(flipped.ravel())
    try:
        img.update()
    except Exception:
        pass
    try:
        img.pack()
    except Exception:
        pass
    if colorspace:
        try:
            img.colorspace_settings.name = colorspace
        except Exception:
            pass
    return img


def load_w2cube_blick_equirect_image(fdir, *, check_existing=True, colorspace='sRGB'):
    """Convert a .w2cube into a packed equirectangular Blick image built from 6 exported face DDS files.

    Returns `(image, dds_path)`. Falls back to `(None, dds_path)` on equirect build failure.
    """
    import os as _os
    from pathlib import Path

    # Ensure the cubemap DDS exists first.
    _unused_img, dds_path = load_w2cube_image(fdir, check_existing=check_existing, colorspace=colorspace)
    if not dds_path or not _os.path.exists(dds_path):
        return None, dds_path

    # Reuse existing exported face files if present; otherwise export them.
    dds_stem = Path(dds_path).stem
    dds_parent = Path(dds_path).parent
    face_files = {}
    for cube_face_key, face_name in _BLICK_EQ_CUBEMAP_FACE_TO_NAME.items():
        suffix = _BLICK_EQ_FACE_W2_SUFFIX[face_name]
        candidate = str(dds_parent / f"{dds_stem}__{suffix}.dds")
        if _os.path.exists(candidate):
            face_files[cube_face_key] = candidate

    if len(face_files) < 6:
        try:
            from .ui_material import _export_cubemap_face_dds_files
            exported = _export_cubemap_face_dds_files(dds_path)
            if exported:
                face_files = exported
        except Exception:
            log.exception("Failed to export cubemap face DDS files for Blick equirect build: %s", dds_path)
            return None, dds_path

    if len(face_files) < 6:
        log.warning("Missing cubemap face DDS files for Blick equirect build: %s", dds_path)
        return None, dds_path

    try:
        faces = _blick_load_face_images_from_dds_files(face_files, check_existing=check_existing)
    except Exception:
        log.exception("Failed loading cubemap face DDS images for Blick equirect build: %s", dds_path)
        return None, dds_path

    if len(faces) < 6:
        missing = sorted(set(_BLICK_EQ_FACE_W2_SUFFIX.keys()) - set(faces.keys()))
        log.warning("Blick equirect build missing faces %s from %s", missing, dds_path)
        return None, dds_path

    try:
        sample = next(iter(faces.values()))
        eq_w = int(sample.shape[1]) * 4
        eq_h = eq_w // 2
        equirect = _blick_cubemap_to_equirectangular_np(faces, eq_w, eq_h)
    except Exception:
        log.exception("Failed converting cubemap faces to Blick equirect image: %s", dds_path)
        return None, dds_path

    image_name = f"{Path(fdir).stem}_BlickCubemap_Equirect"
    try:
        img = _blick_store_equirect_image(equirect, image_name, colorspace=colorspace)
        img["witcher_blick_equirect_source"] = str(fdir)
        img["witcher_blick_equirect_dds"] = str(dds_path)
        return img, dds_path
    except Exception:
        log.exception("Failed creating packed Blick equirect Blender image for %s", dds_path)
        return None, dds_path

