from io import BytesIO
import mmap
import os
import zlib
from pathlib import Path
from typing import List
import numpy as np

from ...bStream import bStream
from .DDS_Metadata import DDSMetadata
from .DDSUtils import DDSUtils
from .DDS_Enums import EFormat

COOKED_CUBEMAP_RGBA8_HV_FLIP_ENABLED = False  # Debug compare vs uncooked raw payload

class CommonImageTools:
    @staticmethod
    def get_eformat_from_redengine_byte(redbyte: int) -> EFormat:
        if redbyte == 0x0 or redbyte == 0xFD:
            return EFormat.R8G8B8A8_UNORM
        elif redbyte == 0x07:
            return EFormat.BC1_UNORM
        elif redbyte == 0x08:
            return EFormat.BC3_UNORM
        elif redbyte == 0x0A:
            return EFormat.BC7_UNORM
        elif redbyte == 0x0D:
            return EFormat.BC2_UNORM
        elif redbyte == 0x0E:
            return EFormat.BC4_UNORM
        elif redbyte == 0x0F:
            return EFormat.BC5_UNORM
        else:
            return EFormat.BC1_UNORM # TEMP, how to deal with .w2l env probes?
            #raise NotImplementedError()

    @staticmethod
    def get_redengine_byte_from_eformat(fmt: EFormat) -> (int, int):
        if fmt == EFormat.R8G8B8A8_UNORM:
            return (0xFD, 0x3)
        elif fmt == EFormat.BC1_UNORM:
            return (0x07, 0x4)
        elif fmt == EFormat.BC2_UNORM:
            return (0x0D, 0x4)
        elif fmt == EFormat.BC3_UNORM:
            return (0x08, 0x4)
        elif fmt == EFormat.BC4_UNORM:
            return (0x0E, 0x4)
        elif fmt == EFormat.BC5_UNORM:
            return (0x0F, 0x4)
        elif fmt == EFormat.BC7_UNORM:
            return (0x0A, 0x4)
        else:
            return (0x0, 0x0)

class MipmapInfo:
    def __init__(self, offset: int, zsize: int, size: int, idx: int):
        self.Offset = offset
        self.Size = size  # unused
        self.ZSize = zsize
        self.Idx = idx  # unused

class TextureCacheItem:
    def __init__(self, parent):
        self.Bundle = parent
        self.DateString: str = None
        self.Format: EFormat = None
        self.ParentFile: str = None
        self.FullName: str = None
        
        self.Name: str = None
        self.Hash: np.uint32 = None
        self.StringTableOffset: int = None
        
        self.PageOffset: int = None
        self.CompressedSize: np.uint32 = None
        self.UncompressedSize: np.uint32 = None
        self.BaseAlignment: np.uint32 = None
        self.BaseWidth: np.uint16 = None
        self.BaseHeight: np.uint16 = None
        self.Mipcount: np.uint16 = None
        self.SliceCount: np.uint16 = None
        self.MipOffsetIndex: int = None
        self.NumMipOffsets: int = None
        self.TimeStamp: int = None
        self.Type1: np.uint8 = None
        self.Type2: np.uint8 = None
        self.IsCube: np.uint8 = None
        self.Unk1: np.uint8 = None
        
        self.Size: np.uint32 = None
        
        self.ZSize: np.uint32 = None
        self.MipIdx: np.uint8 = None
        
        self.MipMapInfo: List[MipmapInfo] = []

    @property
    def name(self):
        return self.Name

    @property
    def CompressionType(self) -> str:
        return "Zlib"

    def switch_red_blue_channels(self, ds):
        for i in range(0, len(ds), 4):
            ds[i], ds[i+2] = ds[i+2], ds[i]  # Switch red (i) and blue (i+2)
        return ds

    def _fix_cubemap_face_orientation(self, face_bytes, width, height):
        """Prepare an RGBA8 cubemap face for DDS export (optional H+V flip + BGRA swizzle).

        REDengine cube face texel orientation for uncompressed RGBA8 cubemaps
        is rotated relative to what Blender/DDS viewers expect. For DDS export,
        this path can reverse pixel order for each face/mip (equivalent to H+V
        flip). The flip is currently toggleable for debugging comparisons with
        uncooked raw payload exports.

        DDSUtils writes an A8R8G8B8-style legacy DDS header for R8G8B8A8_UNORM,
        so swizzle source RGBA bytes to BGRA on output to match the header masks.

        Only applies to uncompressed RGBA8, which can be transformed safely
        without block-compression face-specific logic.
        """
        if self.Format != EFormat.R8G8B8A8_UNORM:
            return face_bytes
        width = int(width or 0)
        height = int(height or 0)
        if width <= 0 or height <= 0:
            return face_bytes

        pixel_count = width * height
        expected_len = pixel_count * 4
        if len(face_bytes) != expected_len:
            return face_bytes

        src = memoryview(face_bytes)
        out = bytearray(expected_len)
        for dst_idx in range(pixel_count):
            if COOKED_CUBEMAP_RGBA8_HV_FLIP_ENABLED:
                src_idx = pixel_count - 1 - dst_idx
            else:
                src_idx = dst_idx
            dst_off = dst_idx * 4
            src_off = src_idx * 4
            out[dst_off + 0] = src[src_off + 2]  # B
            out[dst_off + 1] = src[src_off + 1]  # G
            out[dst_off + 2] = src[src_off + 0]  # R
            out[dst_off + 3] = src[src_off + 3]  # A
        return bytes(out)

    def Extract(self, output_stream:bStream, switch_red_blue = False):
        with open(self.ParentFile, 'rb') as f:
            mmapped_file = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)

            metadata = DDSMetadata(
                    self.BaseWidth,
                    self.BaseHeight,
                    self.Mipcount,
                    self.Format,
                    self.BaseAlignment,
                    self.IsCube == 1,
                    self.SliceCount,
                    False)
            DDSUtils.GenerateAndWriteHeader(output_stream, metadata)
            
            #!PUT THIS INTO FUNCTION TO GENERATE HEADER
            # ddsheader = b'\x44\x44\x53\x20\x7C\x00\x00\x00\x07\x10\x0A\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x20\x00\x00\x00\x05\x00\x00\x00\x44\x58\x54\x31\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x08\x10\x40\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'
            # output_stream.write(ddsheader)
            # output_stream.seek(0xC) #12
            # output_stream.writeUInt16(self.BaseHeight)
            # output_stream.seek(0x10) # 16
            # output_stream.writeUInt16(self.BaseWidth)
            # output_stream.seek(0x1C) # 28
            # output_stream.writeUInt16(self.Mipcount)
            # output_stream.seek(0x54) #84
            # output_stream.write(b"DXT1")#new.write(dxt)
            # output_stream.seek(128)
            
            if self.IsCube == 0:
                offset = self.PageOffset * 4096 + 9
                viewstream = mmapped_file[offset:offset + self.ZSize]
                if switch_red_blue:
                    decompressed_stream = bytearray(zlib.decompress(viewstream))
                    modified_stream = self.switch_red_blue_channels(decompressed_stream)
                    output_stream.write(modified_stream)
                else:
                    output_stream.write(zlib.decompress(viewstream))

                for i in range(self.NumMipOffsets):
                    mippageoffset = self.MipMapInfo[i].Offset
                    mipzsize = self.MipMapInfo[i].ZSize
                    viewstream = mmapped_file[mippageoffset:mippageoffset + mipzsize]
                    output_stream.write(zlib.decompress(viewstream))

            else:
                imagestream = BytesIO()
                mipmapstream = BytesIO()
                mipmapoffsets = []

                # Extract to memory - image
                start = self.PageOffset * 4096 + 9
                viewstream = mmapped_file[start:start + self.ZSize]
                imagestream.write(zlib.decompress(viewstream))

                # Mipmap data <offset, size>
                for mipinfo in self.MipMapInfo:
                    beginoffset = mipmapstream.tell()
                    tempvs = mmapped_file[mipinfo.Offset:mipinfo.Offset + mipinfo.ZSize]
                    mipmapstream.write(zlib.decompress(tempvs))
                    mipmapoffsets.append((beginoffset, mipmapstream.tell() - beginoffset))

                # Assemble faces
                base_w = max(1, int(self.BaseWidth or 1))
                base_h = max(1, int(self.BaseHeight or 1))
                for i in range(6):
                    offset = (i * len(imagestream.getbuffer()) // 6)
                    facesize = len(imagestream.getbuffer()) // 6

                    imagestream.seek(offset)
                    face = imagestream.read(facesize)
                    face = self._fix_cubemap_face_orientation(face, base_w, base_h)
                    output_stream.write(face)

                    # Get mipmaps for face
                    for mip_index, o in enumerate(mipmapoffsets, start=1):
                        mipsize = o[1] // 6
                        moffset = o[0] + (i * mipsize)

                        mipmapstream.seek(moffset)
                        mipmap = mipmapstream.read(mipsize)
                        mip_w = max(1, base_w >> mip_index)
                        mip_h = max(1, base_h >> mip_index)
                        mipmap = self._fix_cubemap_face_orientation(mipmap, mip_w, mip_h)
                        output_stream.write(mipmap)

            mmapped_file.close()
  
    def extract_to_file(self, filepath):
        newpath = Path(filepath).with_suffix('.dds') #make sure dds
        ext = Path(filepath).suffix

        from ...common_blender import win_safe_path

        safe_path = win_safe_path(str(newpath))
        os.makedirs(os.path.dirname(safe_path), exist_ok=True)
        if os.path.exists(safe_path):
            os.unlink(safe_path)

        stream:bStream = bStream(path = safe_path)
        stream.decoder = 'ISO-8859-1'
        self.Extract(stream, ext == '.png')
        stream.close()

    def extract_to_memory(self, filepath):
        ext = Path(filepath).suffix
        stream:bStream = bStream()
        stream.decoder = 'ISO-8859-1'
        self.Extract(stream, ext == '.png')
        #stream.close()
        return stream

    def Write(self, binary_writer):
        # Adaptation of write logic to Python with type hinting
        pass  # Implementation goes here

# Implementations for additional classes or methods should be provided as needed.
