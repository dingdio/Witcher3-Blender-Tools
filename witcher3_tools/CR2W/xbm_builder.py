"""Build a CR2W file containing a CBitmapTexture chunk from raw RGBA8 mip data."""

from collections.abc import Sequence
from datetime import datetime
from types import SimpleNamespace

from .CR2W_types import (
    CR2W,
    CR2W_header,
    CDATETIME,
    CSTRING,
    CR2WExport,
    CR2WProperty,
    DATA,
    PROPERTY,
    W_CLASS,
)
from .Types.VariousTypes import CUInt32, CBytes


def BuildXBM(
    pixel_data: bytes | Sequence[bytes],
    width: int,
    height: int,
    *,
    import_file: str = "",
    import_timestamp: datetime | None = None,
) -> CR2W:
    """Construct a CR2W object with a single CBitmapTexture chunk.

    Args:
        pixel_data: Raw RGBA8 mip bytes in top-to-bottom row order.
                    Can be a single top-level mip or a full mip chain.
        width: Image width in pixels.
        height: Image height in pixels.
        import_file: Source file path recorded in CR2W metadata.
        import_timestamp: Source file timestamp recorded in CR2W metadata.

    Returns:
        A CR2W object ready to be serialized with cr2w_writer.write_xbm().
    """
    mip_payloads = _normalize_mip_payloads(pixel_data)
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

    chunk = _build_cbitmap_chunk(cr2w, mip_payloads, width, height, import_file, import_timestamp)
    cr2w.CHUNKS.CHUNKS.append(chunk)

    return cr2w


def _normalize_mip_payloads(pixel_data: bytes | Sequence[bytes]) -> list[bytes]:
    if isinstance(pixel_data, (bytes, bytearray, memoryview)):
        return [bytes(pixel_data)]
    if not isinstance(pixel_data, Sequence):
        raise TypeError("pixel_data must be raw bytes or a sequence of mip payloads")

    mip_payloads = [bytes(payload) for payload in pixel_data]
    if not mip_payloads:
        raise ValueError("At least one mip payload is required")
    return mip_payloads


def _build_cbitmap_chunk(cr2w, mip_payloads, width, height, import_file, import_timestamp):
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

    cbt = SimpleNamespace()
    cbt.unk = CUInt32(val=0)
    cbt.MipsCount = CUInt32(val=len(mip_payloads))

    mip_width = width
    mip_height = height
    mip_entries = []
    for payload in mip_payloads:
        expected_size = mip_width * mip_height * 4
        if len(payload) != expected_size:
            raise ValueError(
                f"Invalid mip payload size for {mip_width}x{mip_height}: "
                f"expected {expected_size}, got {len(payload)}"
            )

        mip = SimpleNamespace()
        mip.Width = CUInt32(val=mip_width)
        mip.Height = CUInt32(val=mip_height)
        mip.Blocksize = CUInt32(val=mip_width * 4)
        mip.Mip = SimpleNamespace(Bytes=payload)
        mip_entries.append(mip)

        mip_width = max(1, (mip_width + 1) // 2)
        mip_height = max(1, (mip_height + 1) // 2)

    mipdata = SimpleNamespace()
    mipdata.bufferData = mip_entries
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
