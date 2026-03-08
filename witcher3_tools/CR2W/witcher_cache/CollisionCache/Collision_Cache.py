import struct
import os
from typing import List, Dict
from collections import OrderedDict

from ...bStream import bStream
from .CollisionCacheItem import CollisionCacheItem


class InvalidCollisionCacheException(Exception):
    pass


class CollisionCache:
    """
    Parser for Witcher 3 collision.cache files.

    File format:
        Magic: "CC3W" (4 bytes)
        Version: uint32 (1 = 32-bit offsets, 2 = 64-bit offsets)
        Date: 8 bytes
        InfoOffset: uint32/uint64 (depending on version)
        NumberOfFiles: uint32/uint64
        NameTableOffset: uint32/uint64
        NamesSize: uint32
        [Unk3: uint32 - only in v2]
        BufferSize: uint64
        CheckSum: uint64

        Data starts at 0x30 (v1) or 0x40 (v2)
        Name table at NameTableOffset (null-terminated strings)
        Info table at InfoOffset
    """

    MAGIC = b'CC3W'
    BIT_LENGTH_32 = 1  # Version 1 uses 32-bit offsets
    BIT_LENGTH_64 = 2  # Version 2 uses 64-bit offsets
    DATA_OFFSET_V1 = 0x30
    DATA_OFFSET_V2 = 0x40

    def __init__(self, filepath=None):
        self.ArchiveAbsolutePath = filepath
        self.Version = 0
        self.Date = b''
        self.InfoOffset = 0
        self.NumberOfFiles = 0
        self.NameTableOffset = 0
        self.NamesSize = 0
        self.BufferSize = 0
        self.CheckSum = 0

        self.FileNames: List[str] = []
        self.Files: List[CollisionCacheItem] = []
        self.Items: Dict[str, CollisionCacheItem] = OrderedDict()

        if filepath:
            self._read(filepath)

    @property
    def TypeName(self):
        return "CollisionCache"

    def _read(self, filepath: str):
        """Parse collision cache file."""
        with bStream(path=filepath) as f:
            # Read and validate magic
            magic = f.read(4)
            if magic != self.MAGIC:
                raise InvalidCollisionCacheException(
                    f"Invalid collision cache: expected magic 'CC3W', got {magic!r}"
                )

            # Read version
            self.Version = f.readUInt32()
            is_64bit = (self.Version == self.BIT_LENGTH_64)

            # Read date (8 bytes)
            self.Date = f.read(8)

            # Read header fields based on version
            if is_64bit:
                self.InfoOffset = f.readUInt64()
                self.NumberOfFiles = f.readUInt64()
                self.NameTableOffset = f.readUInt64()
            else:
                self.InfoOffset = f.readUInt32()
                self.NumberOfFiles = f.readUInt32()
                self.NameTableOffset = f.readUInt32()

            self.NamesSize = f.readUInt32()

            # v2 has an extra uint32 before BufferSize
            if is_64bit:
                _ = f.readUInt32()  # Unk3

            self.BufferSize = f.readUInt64()
            self.CheckSum = f.readUInt64()

            # Read name table (null-terminated strings)
            f.seek(self.NameTableOffset)
            self.FileNames = []
            for _ in range(self.NumberOfFiles):
                name = f.readStringZero()
                self.FileNames.append(name)

            # Read info table
            f.seek(self.InfoOffset)
            self.Files = []
            self.Items = OrderedDict()

            for i in range(self.NumberOfFiles):
                item = CollisionCacheItem(parent=self)
                item.Name = self.FileNames[i] if i < len(self.FileNames) else ""

                # Read item fields
                item.NameOffset = f.readUInt32()
                item.Unk1 = f.readUInt32()
                item.Unk2 = f.readUInt64()  # Usually null

                if is_64bit:
                    item.PageOffset = f.readUInt64()
                else:
                    item.PageOffset = f.readUInt32()

                item.ZSize = f.readUInt32()
                item.Size = f.readUInt32()
                item.Unk3 = f.readUInt32()
                item.unk4 = f.read(16)
                item.unk5 = f.read(16)
                item.Comtype = f.readUInt8()
                item.Tail = f.read(7)

                self.Files.append(item)

                # Store in dict by name for O(1) lookup
                if item.Name not in self.Items:
                    self.Items[item.Name] = item

    def get_item_by_name(self, name: str) -> CollisionCacheItem:
        """Get a collision cache item by file name."""
        return self.Items.get(name, None)

    def __repr__(self):
        return f"CollisionCache({self.ArchiveAbsolutePath!r}, {len(self.Files)} files)"

    def __len__(self):
        return len(self.Files)

    def __iter__(self):
        return iter(self.Files)
