from io import BytesIO
import mmap
import os
import zlib
import struct
from pathlib import Path
from typing import List, Optional

from ...bStream import bStream
from ...bin_helpers import ReadVLQInt32


def read_length_prefixed_string(data: bytes, offset: int) -> tuple:
    """
    Read a length-prefixed string (1-byte length followed by string bytes).

    Returns:
        tuple: (string, new_offset)
    """
    length = data[offset]
    offset += 1
    string = data[offset:offset + length].decode('utf-8', errors='replace')
    return string, offset + length


def read_vlq_length_prefixed_string(data: bytes, offset: int) -> tuple:
    """
    Read a VLQ-length-prefixed string used by collision.cache wrapper headers.

    Some modded collision cache entries encode string lengths as signed VLQ
    values where the magnitude is the length (e.g. 0x84 -> length 4).
    """
    length, consumed = CollisionCacheItemHeader._read_vlq_from_bytes(data, offset)
    offset += consumed
    length = abs(int(length))
    string = data[offset:offset + length].decode('utf-8', errors='replace')
    return string, offset + length


class CollisionCacheItemHeaderItem:
    """Header item for compound collision files (redcloth, redapex, mesh types 2/3/4)."""
    def __init__(self):
        self.Name: str = ""
        self.Strings: List[str] = []
        self.Unk4: bytes = b''  # 70 bytes
        self.FileSize: int = 0
        self.Flag: int = 0


class CollisionCacheItemHeader:
    """Header for compound collision files that contain multiple sub-files."""
    def __init__(self):
        self.Unk1: int = 0
        self.Unk2: int = 0
        self.Unk3: int = 0
        self.Items: List[CollisionCacheItemHeaderItem] = []

    def read(self, data: bytes) -> int:
        """
        Read header from bytes.

        Returns:
            int: Offset where file data begins
        """
        offset = 0

        self.Unk1 = struct.unpack_from('<I', data, offset)[0]
        offset += 4
        self.Unk2 = struct.unpack_from('<I', data, offset)[0]
        offset += 4
        self.Unk3 = struct.unpack_from('<I', data, offset)[0]
        offset += 4

        count = struct.unpack_from('<I', data, offset)[0]
        offset += 4

        for _ in range(count):
            item = CollisionCacheItemHeaderItem()

            # collision.cache wrapper strings use VLQ-encoded lengths
            item.Name, offset = read_vlq_length_prefixed_string(data, offset)

            # Read VLQ count of strings
            # Simplified VLQ read from bytes
            count2, vlq_bytes = self._read_vlq_from_bytes(data, offset)
            offset += vlq_bytes
            for _ in range(count2):
                s, offset = read_vlq_length_prefixed_string(data, offset)
                item.Strings.append(s)

            item.Unk4 = data[offset:offset + 70]
            offset += 70
            item.FileSize = struct.unpack_from('<I', data, offset)[0]
            offset += 4
            item.Flag = struct.unpack_from('<b', data, offset)[0]
            offset += 1

            self.Items.append(item)

        return offset

    @staticmethod
    def _read_vlq_from_bytes(data: bytes, offset: int) -> tuple:
        """
        Read VLQ int32 from bytes.

        Returns:
            tuple: (value, bytes_consumed)
        """
        b1 = data[offset]
        sign = (b1 & 128) == 128
        next_flag = (b1 & 64) == 64
        size = b1 % 128 % 64
        bit_offset = 6
        consumed = 1

        while next_flag:
            b = data[offset + consumed]
            size = (b % 128) << bit_offset | size
            next_flag = (b & 128) == 128
            bit_offset += 7
            consumed += 1

        if sign:
            return -size, consumed
        return size, consumed


class CollisionCacheItem:
    """
    Files packed into Collision.cache. Zlib compressed nxs/apb/bin files.

    Comtype values:
        1 = w2ter (.bin)
        2 = mesh (.nxs)
        3 = redcloth (.apb)
        4 = redapex (.apb)
        5 = reddest (.nxs)
    """

    # Extension mapping based on Comtype
    EXTENSION_MAP = {
        1: '.bin',   # w2ter
        2: '.nxs',   # mesh
        3: '.apb',   # redcloth
        4: '.apb',   # redapex
        5: '.nxs',   # reddest
    }

    def __init__(self, parent=None):
        self.Bundle = parent          # Parent CollisionCache
        self.Name: str = ""           # File path/name
        self.Size: int = 0            # Uncompressed size
        self.ZSize: int = 0           # Compressed size
        self.PageOffset: int = 0      # Offset to data (can be 32 or 64-bit)
        self.NameOffset: int = 0      # Offset in name table
        self.Unk1: int = 0
        self.Unk2: int = 0            # Usually null/0
        self.Unk3: int = 0
        self.unk4: bytes = b''        # 16 bytes
        self.unk5: bytes = b''        # 16 bytes
        self.Comtype: int = 0         # File type (1-5)
        self.Tail: bytes = b''        # 7 bytes

        self.REDheader: Optional[CollisionCacheItemHeader] = None

    @property
    def CompressionType(self) -> str:
        return "Zlib"

    @property
    def Extension(self) -> str:
        """Get file extension based on Comtype."""
        return self.EXTENSION_MAP.get(self.Comtype, '')

    def Extract(self, output_stream: BytesIO):
        """
        Decompress and write collision data to output stream.

        For Comtype 5 (reddest) and 1 (w2ter), data is directly decompressed.
        For other types (2, 3, 4), there's a RED header with sub-files.
        """
        archive_path = self.Bundle.ArchiveAbsolutePath

        with open(archive_path, 'rb') as f:
            mmapped_file = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)

            try:
                # Read compressed data
                compressed_data = mmapped_file[self.PageOffset:self.PageOffset + self.ZSize]
                decompressed_data = zlib.decompress(compressed_data)

                # For Comtype 5 (reddest) and 1 (w2ter), write directly
                if self.Comtype == 5 or self.Comtype == 1:
                    output_stream.write(decompressed_data)
                else:
                    # Some collision cache entries (observed for mesh/NXS in the
                    # wild) are already raw payloads despite using a Comtype
                    # that normally carries a RED header wrapper.
                    if self.Extension == '.nxs' and decompressed_data.startswith(b'NXS\x01'):
                        output_stream.write(decompressed_data)
                        return

                    # For other types (2, 3, 4), parse RED header and extract sub-files
                    self.REDheader = CollisionCacheItemHeader()
                    try:
                        data_offset = self.REDheader.read(decompressed_data)
                    except Exception as e:
                        raise ValueError(
                            f"Failed to parse RED header for collision cache item '{self.Name}' "
                            f"(Comtype={self.Comtype}, ext='{self.Extension}', "
                            f"decompressed={len(decompressed_data)} bytes)"
                        ) from e

                    # Extract each sub-file
                    offset = data_offset
                    for item in self.REDheader.Items:
                        buffer = decompressed_data[offset:offset + item.FileSize]
                        output_stream.write(buffer)
                        offset += item.FileSize

            finally:
                mmapped_file.close()

    def extract_to_file(self, filepath: str) -> str:
        """
        Extract collision file to disk.

        Args:
            filepath: Target file path (extension will be corrected based on Comtype)

        Returns:
            Actual filepath written (with correct extension)
        """
        # Correct extension based on Comtype
        path = Path(filepath)
        ext = self.Extension
        if ext:
            path = path.with_suffix(ext)

        from ...common_blender import win_safe_path

        safe_path = win_safe_path(str(path))
        safe_tmp = safe_path + '.tmp'

        os.makedirs(os.path.dirname(safe_path), exist_ok=True)

        output = BytesIO()
        self.Extract(output)

        try:
            with open(safe_tmp, 'wb') as f:
                f.write(output.getvalue())
            if os.path.exists(safe_path):
                os.unlink(safe_path)
            os.rename(safe_tmp, safe_path)
        except Exception:
            if os.path.exists(safe_tmp):
                os.unlink(safe_tmp)
            raise

        return str(path)

    def extract_to_memory(self) -> BytesIO:
        """
        Extract collision file to memory.

        Returns:
            BytesIO stream containing decompressed data
        """
        output = BytesIO()
        self.Extract(output)
        output.seek(0)
        return output

    def __repr__(self):
        return f"CollisionCacheItem(Name={self.Name!r}, Comtype={self.Comtype}, Size={self.Size}, ZSize={self.ZSize})"
