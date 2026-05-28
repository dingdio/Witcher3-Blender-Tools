"""Shared DDS layout, header, and payload helpers."""

from __future__ import annotations

import os
import struct

from .bStream import bStream
from .witcher_cache.TextureCache.DDSUtils import DDSUtils
from .witcher_cache.TextureCache.DDS_Metadata import DDSMetadata
from .witcher_cache.TextureCache.DDS_Enums import EFormat


def swizzle_rgba8_bytes_to_bgra(raw_bytes: bytes) -> bytes:
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


def write_dds_payload(dds_path: str, metadata: DDSMetadata, payload: bytes) -> str:
    dir_name = os.path.dirname(dds_path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)

    output_stream = bStream(path=dds_path)
    output_stream.decoder = "ISO-8859-1"
    try:
        DDSUtils.GenerateAndWriteHeader(output_stream, metadata)
        output_stream.write(payload)
    finally:
        output_stream.close()
    return dds_path


def dds_format_name_from_header(data: bytes):
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
        format_name = dds_format_name_from_dxgi(dxgi_format)
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


def dds_format_name_from_dxgi(dxgi_format: int) -> str:
    if dxgi_format in (71, 72):
        return "BC1_UNORM"
    if dxgi_format in (74, 75):
        return "BC2_UNORM"
    if dxgi_format in (77, 78):
        return "BC3_UNORM"
    if dxgi_format == 80:
        return "BC4_UNORM"
    if dxgi_format == 83:
        return "BC5_UNORM"
    if dxgi_format in (28, 29, 87, 91):
        return "R8G8B8A8_UNORM"
    if dxgi_format in (98, 99):
        return "BC7_UNORM"
    raise ValueError(f"Unsupported DXGI format in DDS: {dxgi_format}")


def eformat_from_format_name(format_name: str) -> EFormat:
    format_map = {
        "R8G8B8A8_UNORM": EFormat.R8G8B8A8_UNORM,
        "BC1_UNORM": EFormat.BC1_UNORM,
        "BC2_UNORM": EFormat.BC2_UNORM,
        "BC3_UNORM": EFormat.BC3_UNORM,
        "BC4_UNORM": EFormat.BC4_UNORM,
        "BC5_UNORM": EFormat.BC5_UNORM,
        "BC7_UNORM": EFormat.BC7_UNORM,
    }
    return format_map[format_name]


def dds_top_mip_size(format_name: str, width: int, height: int) -> int:
    return mip_payload_size_for_eformat(eformat_from_format_name(format_name), width, height)


def mip_payload_size_for_eformat(eformat: EFormat, width: int, height: int) -> int:
    width = max(1, int(width))
    height = max(1, int(height))
    if eformat == EFormat.R8G8B8A8_UNORM:
        return width * height * 4

    block_width = max(1, (width + 3) // 4)
    block_height = max(1, (height + 3) // 4)
    if eformat in (EFormat.BC1_UNORM, EFormat.BC4_UNORM):
        return block_width * block_height * 8
    if eformat in (EFormat.BC2_UNORM, EFormat.BC3_UNORM, EFormat.BC5_UNORM, EFormat.BC7_UNORM):
        return block_width * block_height * 16
    raise ValueError(f"Unsupported DDS format: {eformat}")


def row_pitch_for_format_name(format_name: str, width: int) -> int:
    width = int(width)
    if format_name == "R8G8B8A8_UNORM":
        return width * 4

    block_width = max(1, (width + 3) // 4)
    if format_name in ("BC1_UNORM", "BC4_UNORM"):
        return block_width * 8
    if format_name in ("BC2_UNORM", "BC3_UNORM", "BC5_UNORM", "BC7_UNORM"):
        return block_width * 16
    raise ValueError(f"Unsupported DDS format: {format_name}")


def block_bytes_for_eformat(eformat: EFormat):
    if eformat in (EFormat.BC1_UNORM, EFormat.BC4_UNORM):
        return 8
    if eformat in (EFormat.BC2_UNORM, EFormat.BC3_UNORM, EFormat.BC5_UNORM, EFormat.BC7_UNORM):
        return 16
    return None


def is_valid_dds_file(dds_path: str) -> bool:
    try:
        if not dds_path or not os.path.exists(dds_path):
            return False
        with open(dds_path, "rb") as handle:
            header_data = handle.read(148)
        format_name, width, height, _mip_count, data_offset = dds_format_name_from_header(header_data)
        file_size = os.path.getsize(dds_path)
        min_payload = dds_top_mip_size(format_name, width, height)
        return file_size >= (data_offset + min_payload)
    except Exception:
        return False
