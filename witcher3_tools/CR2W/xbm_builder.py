"""Build a CR2W file containing a CBitmapTexture chunk from mip payloads."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from types import SimpleNamespace

from .CR2W_helpers import Enums
from .CR2W_types import (
    CR2W,
    CR2W_header,
    CDATETIME,
    CEnum,
    CSTRING,
    CR2WExport,
    CR2WProperty,
    DATA,
    HANDLE,
    PROPERTY,
    W_CLASS,
)
from .Types.VariousTypes import CUInt32, CBytes


def BuildXBM(
    pixel_data,
    width: int,
    height: int,
    *,
    import_file: str = "",
    import_timestamp: datetime | None = None,
    compression: str = Enums.ETextureCompression.TCM_None.name,
    texture_group: str = "Default",
) -> CR2W:
    """Construct a CR2W object with a single CBitmapTexture chunk."""
    mip_entries = _normalize_mip_entries(pixel_data, width, height, compression)
    import_timestamp = import_timestamp or datetime.now()

    cr2w = CR2W()
    cr2w.CNAMES = []
    cr2w.HEADER = CR2W_header(
        CRC32=0,
        bufferSize=0,
        buildVersion=9908608,
        fileSize=0,
        flags=0,
        magic=0x57325243,
        numChunks=1,
        timestamp=0,
        version=163,
    )

    cr2w.CR2WImport = []
    cr2w.CR2W_Property = [CR2WProperty()]
    cr2w.CR2WBuffer = []
    cr2w.BufferData = []

    cr2w.CHUNKS = DATA()
    cr2w.CR2WExport = []

    chunk = _build_cbitmap_chunk(
        cr2w,
        mip_entries,
        width,
        height,
        import_file,
        import_timestamp,
        compression,
        texture_group,
    )
    cr2w.CHUNKS.CHUNKS.append(chunk)

    return cr2w


def _normalize_mip_entries(pixel_data, width: int, height: int, compression: str) -> list[dict]:
    if isinstance(pixel_data, (bytes, bytearray, memoryview)):
        payloads = [bytes(pixel_data)]
    elif isinstance(pixel_data, Sequence):
        payloads = list(pixel_data)
    else:
        raise TypeError("pixel_data must be raw bytes or a sequence of mip payloads")

    if not payloads:
        raise ValueError("At least one mip payload is required")

    mip_entries = []
    mip_width = int(width)
    mip_height = int(height)
    for payload in payloads:
        if isinstance(payload, Mapping):
            mip_bytes = bytes(payload.get("bytes", b""))
            mip_width = int(payload.get("width", mip_width))
            mip_height = int(payload.get("height", mip_height))
            blocksize = int(payload.get("blocksize", _default_blocksize(mip_width, compression)))
        else:
            mip_bytes = bytes(payload)
            blocksize = _default_blocksize(mip_width, compression)

        expected_size = _expected_payload_size(mip_width, mip_height, compression)
        if len(mip_bytes) != expected_size:
            raise ValueError(
                f"Invalid mip payload size for {mip_width}x{mip_height} {compression}: "
                f"expected {expected_size}, got {len(mip_bytes)}"
            )

        mip_entries.append({
            "width": mip_width,
            "height": mip_height,
            "blocksize": blocksize,
            "bytes": mip_bytes,
        })

        mip_width = max(1, (mip_width + 1) // 2)
        mip_height = max(1, (mip_height + 1) // 2)

    return mip_entries


def _expected_payload_size(width: int, height: int, compression: str) -> int:
    width = int(width)
    height = int(height)
    block_width = max(1, (width + 3) // 4)
    block_height = max(1, (height + 3) // 4)

    if compression == Enums.ETextureCompression.TCM_None.name:
        return width * height * 4
    if compression in (
        Enums.ETextureCompression.TCM_DXTNoAlpha.name,
        Enums.ETextureCompression.TCM_Normals.name,
        Enums.ETextureCompression.TCM_QualityR.name,
    ):
        return block_width * block_height * 8
    if compression in (
        Enums.ETextureCompression.TCM_DXTAlpha.name,
        Enums.ETextureCompression.TCM_NormalsHigh.name,
        Enums.ETextureCompression.TCM_NormalsGloss.name,
        Enums.ETextureCompression.TCM_QualityRG.name,
        Enums.ETextureCompression.TCM_QualityColor.name,
    ):
        return block_width * block_height * 16
    raise NotImplementedError(f"Unsupported XBM compression for export: {compression}")


def _default_blocksize(width: int, compression: str) -> int:
    width = int(width)
    block_width = max(1, (width + 3) // 4)

    if compression == Enums.ETextureCompression.TCM_None.name:
        return width * 4
    if compression in (
        Enums.ETextureCompression.TCM_DXTNoAlpha.name,
        Enums.ETextureCompression.TCM_Normals.name,
        Enums.ETextureCompression.TCM_QualityR.name,
    ):
        return block_width * 8
    if compression in (
        Enums.ETextureCompression.TCM_DXTAlpha.name,
        Enums.ETextureCompression.TCM_NormalsHigh.name,
        Enums.ETextureCompression.TCM_NormalsGloss.name,
        Enums.ETextureCompression.TCM_QualityRG.name,
        Enums.ETextureCompression.TCM_QualityColor.name,
    ):
        return block_width * 16
    raise NotImplementedError(f"Unsupported XBM compression for export: {compression}")


def _make_enum(cr2w, value: str) -> CEnum:
    enum_obj = CEnum(cr2w)
    enum_obj.String = value
    enum_obj.strings = [value]
    return enum_obj


def _make_null_handle(cr2w, handle_type: str) -> HANDLE:
    return HANDLE(
        CR2WFILE=cr2w,
        ChunkHandle=False,
        ClassName=None,
        DepotPath=None,
        Flags=0,
        Index=None,
        Reference=None,
        theType=handle_type,
        val=0,
    )


def _build_cbitmap_chunk(
    cr2w,
    mip_entries,
    width,
    height,
    import_file,
    import_timestamp,
    compression,
    texture_group,
):
    """Build the CBitmapTexture W_CLASS chunk with properties and binary data."""
    cr2w.CR2WExport.append(CR2WExport(
        crc32=0,
        dataOffset=0,
        dataSize=0,
        name='CBitmapTexture',
        objectFlags=0,
        parentID=0,
        template=0,
    ))

    chunk = W_CLASS(
        CR2WFILE=cr2w,
        idx=0,
        PROPS=[],
        Type='CBitmapTexture',
        name='CBitmapTexture',
    )

    chunk.PROPS.append(PROPERTY(
        theName='importFile',
        theType='String',
        String=CSTRING(isUTF=False, String=import_file),
    ))
    chunk.PROPS.append(PROPERTY(
        theName='importFileTimeStamp',
        theType='CDateTime',
        DateTime=_make_cdatetime(import_timestamp),
    ))
    chunk.PROPS.append(PROPERTY(theName='width', theType='Uint32', Value=width))
    chunk.PROPS.append(PROPERTY(theName='height', theType='Uint32', Value=height))
    chunk.PROPS.append(PROPERTY(
        theName='compression',
        theType='ETextureCompression',
        Index=_make_enum(cr2w, compression),
    ))
    chunk.PROPS.append(PROPERTY(
        theName='sourceData',
        theType='handle:CSourceTexture',
        Handles=[_make_null_handle(cr2w, 'handle:CSourceTexture')],
    ))
    chunk.PROPS.append(PROPERTY(
        theName='textureGroup',
        theType='CName',
        String=CSTRING(isUTF=False, String=texture_group),
    ))

    cbt = SimpleNamespace()
    cbt.unk = CUInt32(val=0)
    cbt.MipsCount = CUInt32(val=len(mip_entries))

    normalized_mips = []
    for mip_info in mip_entries:
        mip = SimpleNamespace()
        mip.Width = CUInt32(val=mip_info["width"])
        mip.Height = CUInt32(val=mip_info["height"])
        mip.Blocksize = CUInt32(val=mip_info["blocksize"])
        mip.Mip = SimpleNamespace(Bytes=mip_info["bytes"])
        normalized_mips.append(mip)

    mipdata = SimpleNamespace()
    mipdata.bufferData = normalized_mips
    cbt.Mipdata = mipdata

    cbt.ResidentmipSize = CUInt32(val=0)
    cbt.unk1 = None
    cbt.unk2 = None
    cbt.Residentmip = CBytes(val=None)

    chunk.CBitmapTexture = cbt

    return chunk


def _make_cdatetime(value: datetime) -> CDATETIME:
    timestamp_value = _encode_cdatetime_value(value)
    return CDATETIME(Value=timestamp_value, String=value.strftime("%Y/%m/%d %H:%M:%S"))


def _encode_cdatetime_value(value: datetime) -> int:
    encoded = value.hour & 0x1FF
    encoded = (encoded << 6) | (value.minute & 0x3F)
    encoded = (encoded << 6) | (value.second & 0x3F)
    encoded = (encoded << 10) | ((value.microsecond // 1000) & 0x3FF)
    encoded = (encoded << 12) | (value.year & 0xFFF)
    encoded = (encoded << 5) | ((value.month - 1) & 0x1F)
    encoded = (encoded << 5) | ((value.day - 1) & 0x1F)
    encoded <<= 10
    return encoded
