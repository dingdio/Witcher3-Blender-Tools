"""Cooked/source REDengine texture conversion helpers."""

from __future__ import annotations

import logging
import os
import struct

from .bStream import bStream, bReadStream, open_cr2w_read_stream
from .CR2W_helpers import Enums
from .CR2W_types import getCR2W
from .texture_dds import (
    DDSMetadata,
    EFormat,
    block_bytes_for_eformat,
    dds_format_name_from_header,
    eformat_from_format_name,
    is_valid_dds_file,
    mip_payload_size_for_eformat,
    swizzle_rgba8_bytes_to_bgra,
    write_dds_payload,
)
from .witcher_cache.TextureCache import LoadTextureManager
from .witcher_cache.TextureCache.TextureCacheItem import CommonImageTools

log = logging.getLogger(__name__)

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


class ImageUtility():
    @staticmethod
    def GetEFormatFromCompression(compression: str, source_game: str = "w3"):
        if compression == Enums.ETextureCompression.TCM_None.name:
            return EFormat.R8G8B8A8_UNORM
        elif compression in (Enums.ETextureCompression.TCM_DXTNoAlpha.name, Enums.ETextureCompression.TCM_Normals.name):
            return EFormat.BC1_UNORM
        elif compression in (Enums.ETextureCompression.TCM_DXTAlpha.name, Enums.ETextureCompression.TCM_NormalsHigh.name, Enums.ETextureCompression.TCM_NormalsGloss.name):
            return EFormat.BC3_UNORM
        elif compression == Enums.ETextureCompression.TCM_QualityColor.name:
            if str(source_game or "").lower() in {"witcher2", "tw2"}:
                return EFormat.BC3_UNORM
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

    ddsformat = ImageUtility.GetEFormatFromCompression(dxt)
    return DDSMetadata(width, height, mipcount, ddsformat)


def _find_witcher2_xbm_mip_table_start(data: bytes, start_offset: int, width: int | None, height: int | None) -> int:
    search_end = min(len(data) - 20, start_offset + 48)
    for offset in range(max(0, start_offset), max(0, search_end)):
        unk, mip_count, mip_width, mip_height, _blocksize = struct.unpack_from("<IIIII", data, offset)
        if unk != 0 or not (1 <= mip_count <= 32):
            continue
        if width is not None and mip_width != width:
            continue
        if height is not None and mip_height != height:
            continue
        return offset
    return -1


def _convert_witcher2_xbm_to_dds_raw(fdir, dds_path, xbm_file, texture_chunk, force=False):
    """Convert old Witcher 2 XBM by extracting inline mip payloads from the parsed texture chunk."""
    if os.path.exists(dds_path) and not force:
        return dds_path

    with open(fdir, "rb") as handle:
        data = handle.read()

    if len(data) < 44 or data[:4] != b"CR2W":
        raise ValueError("Not a Witcher 2 CR2W texture")

    export = xbm_file.CR2WExport[texture_chunk.ChunkIndex]
    chunk_end = min(len(data), int(getattr(xbm_file, "start", 0) or 0) + export.dataOffset + export.dataSize)
    prop_ends = [int(getattr(prop, "dataEnd", 0) or 0) for prop in getattr(texture_chunk, "PROPS", []) or []]
    prop_end = max(prop_ends) if prop_ends else int(getattr(xbm_file, "start", 0) or 0) + export.dataOffset

    width_var = texture_chunk.GetVariableByName("width")
    height_var = texture_chunk.GetVariableByName("height")
    compression_var = texture_chunk.GetVariableByName("compression")
    width = getattr(width_var, "Value", None)
    height = getattr(height_var, "Value", None)
    compression = getattr(getattr(compression_var, "Index", None), "String", "") if compression_var else ""
    if not width or not height or not compression:
        raise ValueError("Incomplete Witcher 2 XBM texture metadata")

    mip_table_start = _find_witcher2_xbm_mip_table_start(data, prop_end, width, height)
    if mip_table_start < 0:
        raise ValueError("Witcher 2 XBM mip table not found")

    _unknown, mip_count = struct.unpack_from("<II", data, mip_table_start)
    offset = mip_table_start + 8
    payload_parts = []
    first_width = width
    first_height = height
    for mip_index in range(mip_count):
        mip_width, mip_height, _blocksize, mip_size = struct.unpack_from("<IIII", data, offset)
        offset += 16
        if mip_index == 0:
            first_width = mip_width
            first_height = mip_height
        if mip_size < 0 or offset + mip_size > chunk_end:
            raise ValueError("Invalid W2 XBM mip payload size")
        payload_parts.append(data[offset:offset + mip_size])
        offset += mip_size

    metadata = DDSMetadata(
        int(first_width),
        int(first_height),
        int(mip_count),
        ImageUtility.GetEFormatFromCompression(compression, source_game="witcher2"),
    )
    payload = b"".join(payload_parts)
    if metadata.format == EFormat.R8G8B8A8_UNORM:
        payload = swizzle_rgba8_bytes_to_bgra(payload)
    return write_dds_payload(dds_path, metadata, payload)


def _texture_cache_extract_path(texture_item, out_path):
    if out_path:
        return out_path

    import bpy
    from .. import get_texture_path

    return os.path.join(get_texture_path(bpy.context), texture_item.Name)


def convert_xbm_to_dds(fdir, force=False, out_path=None):
    f = open_cr2w_read_stream(fdir)
    xbmFile = getCR2W(f)

    br = bStream(data=f.cr2w_buf)
    f.close()

    ddsheader = b'\x44\x44\x53\x20\x7C\x00\x00\x00\x07\x10\x0A\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x20\x00\x00\x00\x05\x00\x00\x00\x44\x58\x54\x31\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x08\x10\x40\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'

    dds_path = os.path.splitext(out_path or fdir)[0] + '.dds'
    for chunk in xbmFile.CHUNKS.CHUNKS:
        if chunk.Type != "CBitmapTexture":
            continue

        if xbmFile.HEADER.version <= 115:
            try:
                return _convert_witcher2_xbm_to_dds_raw(fdir, dds_path, xbmFile, chunk, force=force)
            except Exception:
                log.debug("Raw Witcher 2 XBM conversion failed, falling back to CR2W props: %s", fdir, exc_info=True)

        width_var = chunk.GetVariableByName('width')
        height_var = chunk.GetVariableByName('height')
        if width_var is None or height_var is None:
            raise ValueError("XBM texture is missing width/height metadata")
        width = width_var.Value
        height = height_var.Value

        if xbmFile.HEADER.version > 115:
            mipcount = 1
            residentMipIndex = chunk.GetVariableByName('residentMipIndex').Value if chunk.GetVariableByName('residentMipIndex') else 0
            mipcount = len(chunk.CBitmapTexture.Mipdata.bufferData) - residentMipIndex
            width = chunk.CBitmapTexture.Mipdata.bufferData[residentMipIndex].Width.val
            height = chunk.CBitmapTexture.Mipdata.bufferData[residentMipIndex].Height.val
            dxt = chunk.GetVariableByName('compression').Index.String if chunk.GetVariableByName('compression') else "TCM_None"
            textureCacheKey = chunk.GetVariableByName('textureCacheKey')
            metadata = GetDDSMetadata(chunk)
            packed_mipcount = struct.pack('i', mipcount)
        else:
            dxt = chunk.GetVariableByName('compression').Index.String if chunk.GetVariableByName('compression') else "TCM_None"
            packed_mipcount = struct.pack('i', 1)
            metadata = None
            textureCacheKey = None
        packed_width = struct.pack('i', width)
        packed_height = struct.pack('i', height)

        if dxt in ['TCM_DXTNoAlpha', 'TCM_Normals']:
            packed_dxt = b'\x44\x58\x54\x31'
        elif dxt == 'TCM_None':
            packed_dxt = b'\x00\x00\x00\x00'
        elif dxt in ['TCM_DXTAlpha', 'TCM_NormalsHigh', 'TCM_NormalsGloss', 'TCM_QualityColor']:
            packed_dxt = b'\x44\x58\x54\x35'
        else:
            raise ValueError(f"Unknown DXT compression: {dxt}")

        if os.path.exists(dds_path) and not force:
            return dds_path
        br.seek(chunk.PROPS[-1].dataEnd)

        if xbmFile.HEADER.version <= 115:
            br.seek(27, 1)
            with open(dds_path, 'wb') as new:
                new.write(ddsheader)
                new.seek(0xC)
                new.write(packed_height)
                new.seek(0x10)
                new.write(packed_width)
                new.seek(0x54)
                new.write(packed_dxt)
                new.seek(128)
                new.write(br.read(None))
        else:
            export_from_texture_cache = True
            is_cooked = any(export.objectFlags == 8192 and export.name == 'CBitmapTexture' for export in xbmFile.CR2WExport)

            if is_cooked and export_from_texture_cache:
                texture_item = None
                # Try vanilla first (common case), then fall back to mods.
                for use_mods in (False, True):
                    texture_manager = LoadTextureManager(loadmods=use_mods)
                    item = texture_manager.find_item_by_hash(textureCacheKey.Value)
                    if item:
                        texture_item = item[-1]  # last bundle loaded, should be top mod
                        break
                if texture_item:
                    extractPath = _texture_cache_extract_path(texture_item, out_path)
                    texture_item.extract_to_file(extractPath)
                    dds_path = os.path.splitext(extractPath)[0] + '.dds'
                else:
                    log.debug("Uncooked Texture not found in cache, using cooked file. %s", fdir)
                    if is_cooked:
                        payload = chunk.CBitmapTexture.Residentmip.val
                    else:
                        if len(chunk.CBitmapTexture.Mipdata.bufferData) <= 0:
                            return None
                        payload = b''.join(buff.Mip.Bytes for buff in chunk.CBitmapTexture.Mipdata.bufferData)

                    if metadata.format == EFormat.R8G8B8A8_UNORM:
                        payload = swizzle_rgba8_bytes_to_bgra(payload)
                    write_dds_payload(dds_path, metadata, payload)
            else:
                if is_cooked:
                    payload = chunk.CBitmapTexture.Residentmip.val
                else:
                    if len(chunk.CBitmapTexture.Mipdata.bufferData) <= 0:
                        return None
                    payload = b''.join(buff.Mip.Bytes for buff in chunk.CBitmapTexture.Mipdata.bufferData)

                if metadata and metadata.format == EFormat.R8G8B8A8_UNORM:
                    payload = swizzle_rgba8_bytes_to_bgra(payload)

                if metadata is not None:
                    write_dds_payload(dds_path, metadata, payload)
                else:
                    with open(dds_path, 'wb') as new:
                        new.write(ddsheader)
                        new.seek(0xC)
                        new.write(packed_height)
                        new.seek(0x10)
                        new.write(packed_width)
                        new.seek(0x1C)
                        new.write(packed_mipcount)
                        new.seek(0x54)
                        new.write(packed_dxt)
                        new.seek(128)
                        new.write(payload)

        break
    return dds_path


def _split_texture_array_mip_major_payload(
        payload: bytes,
        width: int,
        height: int,
        mip_count: int,
        slice_count: int,
        eformat: EFormat,
        ) -> list[bytes]:
    """Split REDengine texture-array bytes into per-slice DDS payloads.

    TextureCache stores arrays mip-major: mip 0 for every slice, then mip 1 for
    every slice, and so on. Blender image nodes need ordinary 2D textures, so
    rebuild each slice as its own full mip chain.
    """
    width = max(1, int(width))
    height = max(1, int(height))
    mip_count = max(1, int(mip_count))
    slice_count = max(1, int(slice_count))

    src = memoryview(payload)
    cursor = 0
    slices = [bytearray() for _ in range(slice_count)]
    mip_width = width
    mip_height = height

    for _mip_index in range(mip_count):
        mip_slice_size = mip_payload_size_for_eformat(eformat, mip_width, mip_height)
        mip_total_size = mip_slice_size * slice_count
        if cursor + mip_total_size > len(src):
            return []

        for slice_index in range(slice_count):
            start = cursor + (slice_index * mip_slice_size)
            slices[slice_index].extend(src[start:start + mip_slice_size])

        cursor += mip_total_size
        mip_width = max(1, mip_width // 2)
        mip_height = max(1, mip_height // 2)

    if cursor != len(src):
        return []
    return [bytes(slice_payload) for slice_payload in slices]


def _write_texture_array_slices(
        texarray_path: str,
        width: int,
        height: int,
        mip_count: int,
        eformat: EFormat,
        slice_payloads: list[bytes],
        *,
        force: bool = False,
        ) -> list[str]:
    output_paths = []
    metadata = DDSMetadata(
        width=int(width),
        height=int(height),
        mipscount=int(mip_count),
        format=eformat,
    )

    for slice_index, payload in enumerate(slice_payloads):
        output_path = f"{texarray_path}.texture_{slice_index}.dds"
        output_paths.append(output_path)
        if os.path.exists(output_path) and not force and is_valid_dds_file(output_path):
            continue
        if eformat == EFormat.R8G8B8A8_UNORM:
            payload = swizzle_rgba8_bytes_to_bgra(payload)
        write_dds_payload(output_path, metadata, payload)

    return output_paths


def _extract_texture_cache_array_slices(
        texarray_path: str,
        texturecachekey: int,
        fallback_slice_count: int,
        *,
        force: bool = False,
        ) -> list[str]:
    if not texturecachekey:
        return []

    for use_mods in (False, True):
        try:
            texture_manager = LoadTextureManager(loadmods=use_mods)
            items = texture_manager.find_item_by_hash(texturecachekey)
        except Exception:
            log.debug("TextureCache lookup failed for texarray key %s", texturecachekey, exc_info=True)
            continue

        if not items:
            continue

        texture_item = items[-1]
        slice_count = int(getattr(texture_item, "SliceCount", 0) or fallback_slice_count or 0)
        if slice_count <= 0:
            continue

        try:
            stream = bStream(data=b"")
            stream.decoder = 'ISO-8859-1'
            texture_item.Extract(stream)
            dds_bytes = stream.fhandle.getvalue()
            format_name, width, height, mip_count, data_offset = dds_format_name_from_header(dds_bytes[:148])
            eformat = eformat_from_format_name(format_name)
            slice_payloads = _split_texture_array_mip_major_payload(
                dds_bytes[data_offset:],
                width,
                height,
                mip_count,
                slice_count,
                eformat,
            )
            if slice_payloads:
                return _write_texture_array_slices(
                    texarray_path,
                    width,
                    height,
                    mip_count,
                    eformat,
                    slice_payloads,
                    force=force,
                )
        except Exception:
            log.debug("Failed to extract texarray %s from TextureCache", texarray_path, exc_info=True)

    return []


def convert_texarray_to_dds(fdir: str, force: bool = False) -> list[str]:
    """Convert a cooked CTextureArray into per-slice DDS files.

    Returns paths named like `foo.texarray.texture_0.dds`. TextureCache is used
    first for full resolution; the embedded resident mips are used as fallback.
    """
    if not fdir or not os.path.isfile(fdir):
        return []

    with open(fdir, "rb") as handle:
        file_bytes = handle.read()

    with open(fdir, "rb") as handle:
        texarray_file = getCR2W(handle)

    br = bStream(data=file_bytes)
    file_size = len(file_bytes)

    for chunk in texarray_file.CHUNKS.CHUNKS:
        if getattr(chunk, "Type", "") != "CTextureArray":
            continue
        if not getattr(chunk, "PROPS", None):
            continue

        buffer_start = getattr(chunk, 'texarray_buffer_start', None)
        if buffer_start is None:
            buffer_start = chunk.PROPS[-1].dataEnd
        if buffer_start is None or buffer_start < 0 or buffer_start >= file_size:
            continue

        candidate_starts = [int(buffer_start)]
        if getattr(chunk, 'texarray_buffer_start', None) is None:
            candidate_starts.extend(int(buffer_start) + offset for offset in (2, 4, 8, 12, 16))

        seen_starts = set()
        for candidate_start in candidate_starts:
            if candidate_start in seen_starts or candidate_start < 0 or candidate_start + 24 > file_size:
                continue
            seen_starts.add(candidate_start)

            br.seek(candidate_start)
            texturecachekey = br.readUInt32()
            encodedformat = br.readUInt16()
            width = br.readUInt16()
            height = br.readUInt16()
            slice_count = br.readUInt16()
            mipmapscount = br.readUInt16()
            residentmip = br.readUInt16()
            filesize = br.readUInt32()
            sentinel = br.readInt32()

            data_pos = br.tell()
            remaining = max(0, file_size - data_pos)
            header_plausible = (
                bool(width) and bool(height) and
                1 <= int(slice_count or 0) <= 256 and
                1 <= int(mipmapscount or 0) <= 32 and
                0 <= int(residentmip or 0) <= int(mipmapscount or 0) and
                0 <= int(filesize or 0) <= remaining and
                sentinel == -1
            )
            if not header_plausible:
                continue

            converted_paths = _extract_texture_cache_array_slices(
                fdir,
                int(texturecachekey or 0),
                int(slice_count or 0),
                force=force,
            )
            if converted_paths:
                return converted_paths

            raw_bytes = br.read(filesize)
            eformat = CommonImageTools.get_eformat_from_redengine_byte(encodedformat & 0xFF)
            actual_width = max(1, int(width) >> int(residentmip or 0))
            actual_height = max(1, int(height) >> int(residentmip or 0))
            actual_mips = max(1, int(mipmapscount) - int(residentmip or 0))
            slice_payloads = _split_texture_array_mip_major_payload(
                raw_bytes,
                actual_width,
                actual_height,
                actual_mips,
                int(slice_count),
                eformat,
            )
            if slice_payloads:
                return _write_texture_array_slices(
                    fdir,
                    actual_width,
                    actual_height,
                    actual_mips,
                    eformat,
                    slice_payloads,
                    force=force,
                )

    return []


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
    block_bytes = block_bytes_for_eformat(eformat)
    if block_bytes:
        return _block_compressed_full_mip_chain_face_size(edge, block_bytes)
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
    """Detect raw 6-face full-mip cubemap payload packed at end of an uncooked .w2cube."""
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
        block_bytes = block_bytes_for_eformat(eformat)
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
    metadata = DDSMetadata(
        width=edge,
        height=edge,
        mipscount=mip_count,
        format=eformat,
        iscubemap=True,
        slicecount=6,
        normal=False
    )
    write_dds_payload(dds_path, metadata, raw_bytes)

    log.info(
        "convert_w2cube_to_dds: exported uncooked raw cubemap tail payload to DDS (%s, edge=%s, mips=%s, payload_start=%s, eformat=%s, variant=%s, alpha_byte=%s, alpha_counts=%s, hint=%s, layout=%s)",
        dds_path, edge, mip_count, payload_start, getattr(eformat, 'name', eformat), fmt_name, alpha_byte, alpha_counts, layout.get("compression_hint"), layout_note
    )
    return dds_path


#! TODO need to fix this for Witcher 2 cubemaps. Temp hack
def convert_w2cube_to_dds(fdir):
    """Parse a cooked .w2cube file and write a cubemap DDS."""
    # The output path is deterministic, so if a valid DDS already exists reuse it
    # without re-reading/re-parsing the .w2cube. The same shared cubemap (e.g.
    # metal1.w2cube) is referenced by many materials on one character; without
    # this guard every reference re-exported the DDS from scratch.
    dds_path = fdir.replace('.w2cube', '_cubemap.dds')
    if os.path.exists(dds_path) and is_valid_dds_file(dds_path):
        return dds_path

    # Cache .w2cube files that previously raised (e.g. uncooked source cubes like
    # dot3.w2cube with no runtime payload) so repeated material references don't
    # re-parse and re-raise the same failure on every slot.
    norm_key = os.path.normcase(os.path.abspath(fdir))
    if norm_key in _W2CUBE_CONVERT_FAILED:
        raise _W2CUBE_CONVERT_FAILED[norm_key]

    try:
        return _convert_w2cube_to_dds_impl(fdir, dds_path)
    except Exception as exc:
        _W2CUBE_CONVERT_FAILED[norm_key] = exc
        raise


_W2CUBE_CONVERT_FAILED = {}  # normalized .w2cube path -> exception raised on first attempt


def _convert_w2cube_to_dds_impl(fdir, dds_path):
    with open(fdir, "rb") as f:
        file_bytes = f.read()
    file_size = len(file_bytes)

    # Uncooked/source cubemaps can store a raw full cubemap payload at the end
    # of the file. Export them directly without invoking the CR2W parser so the
    # import path matches cooked assets (DDS -> face previews).
    uncooked_dds = _write_uncooked_w2cube_raw_tail_payload_to_dds(fdir, file_bytes)
    if uncooked_dds:
        return uncooked_dds

    w2cubeFile = getCR2W(bReadStream(file_bytes, name=fdir))
    br = bStream(data=file_bytes)

    for chunk in w2cubeFile.CHUNKS.CHUNKS:
        if chunk.Type != "CCubeTexture":
            continue

        buffer_start = getattr(chunk, 'cube_buffer_start', None)
        if buffer_start is None:
            if chunk.PROPS:
                buffer_start = chunk.PROPS[-1].dataEnd
            else:
                continue
        br.seek(buffer_start)

        texturecachekey = br.readUInt32()
        residentmip = br.readUInt16()
        encodedformat = br.readUInt16()
        edge = br.readUInt16()
        mipmapscount = br.readUInt16()
        filesize = br.readUInt32()
        sentinel = br.readInt32()

        data_pos = br.tell()
        remaining = max(0, file_size - data_pos)
        edge_is_pow2 = edge > 0 and (edge & (edge - 1)) == 0
        header_plausible = (
            edge_is_pow2 and
            1 <= edge <= 16384 and
            1 <= mipmapscount <= 32 and
            0 <= residentmip <= mipmapscount and
            0 <= filesize <= remaining and
            sentinel == -1
        )
        if not header_plausible:
            raise ValueError(
                "CCubeTexture does not contain a cooked runtime cubemap payload "
                f"(likely uncooked/source .w2cube). Parsed header: key={texturecachekey}, "
                f"residentmip={residentmip}, format=0x{encodedformat & 0xFFFF:04X}, "
                f"edge={edge}, mips={mipmapscount}, filesize={filesize}, sentinel={sentinel}"
            )

        # Strategy 1: full resolution from TextureCache.
        if texturecachekey:
            for use_mods in (False, True):
                try:
                    texture_manager = LoadTextureManager(loadmods=use_mods)
                    items = texture_manager.find_item_by_hash(texturecachekey)
                    if items:
                        texture_item = items[-1]
                        texture_item.extract_to_file(dds_path)
                        if os.path.exists(dds_path):
                            return dds_path
                except Exception:
                    pass

        # Strategy 2: embedded low-res data (residentmip onwards).
        raw_bytes = br.read(filesize)
        format_byte = encodedformat & 0xFF
        eformat = CommonImageTools.get_eformat_from_redengine_byte(format_byte)

        actual_edge = edge >> residentmip if residentmip > 0 else edge
        actual_mips = mipmapscount - residentmip
        if eformat == EFormat.R8G8B8A8_UNORM:
            raw_bytes = _flip_rgba8_cubemap_faces_hv(raw_bytes, actual_edge, actual_mips)

        metadata = DDSMetadata(
            width=actual_edge,
            height=actual_edge,
            mipscount=actual_mips,
            format=eformat,
            iscubemap=True,
            slicecount=6,
            normal=False
        )
        write_dds_payload(dds_path, metadata, raw_bytes)
        return dds_path

    return None
