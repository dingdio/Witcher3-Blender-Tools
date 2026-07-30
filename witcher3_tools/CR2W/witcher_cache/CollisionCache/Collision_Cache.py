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
        Version: uint32 (supported layouts: 1, 2, 9, and 10)
        Date: 8 bytes
        InfoOffset: uint32 (v1/v9) or uint64 (v2/v10)
        NumberOfFiles: uint32 except legacy v2's uint64
        NameTableOffset: uint32 (v1/v9) or uint64 (v2/v10)
        NamesSize: uint32
        ReadBufferSize: uint32
        LoadBufferSize: uint32
        CheckSum: uint64

        Data starts at 0x30 (v1/v9), 0x40 (v2), or 0x38 (v10)
        Name table at NameTableOffset (null-terminated strings)
        Info table at InfoOffset
    """

    MAGIC = b'CC3W'
    VERSION_1 = 1
    VERSION_2 = 2
    VERSION_9 = 9
    VERSION_10 = 10
    BIT_LENGTH_32 = VERSION_1  # Compatibility aliases for existing callers.
    BIT_LENGTH_64 = VERSION_2
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
        self.ReadBufferSize = 0
        self.LoadBufferSize = 0
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
        with open(filepath, "rb") as handle, bStream(path=filepath, reader=handle) as f:
            # Read and validate magic
            magic = f.read(4)
            if magic != self.MAGIC:
                raise InvalidCollisionCacheException(
                    f"Invalid collision cache: expected magic 'CC3W', got {magic!r}"
                )

            # Read version
            self.Version = f.readUInt32()
            if self.Version not in (self.VERSION_1, self.VERSION_2, self.VERSION_9, self.VERSION_10):
                raise InvalidCollisionCacheException(
                    f"Unsupported collision cache version {self.Version}; expected 1, 2, 9, or 10"
                )

            # Read date (8 bytes)
            self.Date = f.read(8)

            # Read header fields based on version
            if self.Version == self.VERSION_2:
                self.InfoOffset = f.readUInt64()
                self.NumberOfFiles = f.readUInt64()
                self.NameTableOffset = f.readUInt64()
            elif self.Version == self.VERSION_10:
                self.InfoOffset = f.readUInt64()
                self.NumberOfFiles = f.readUInt32()
                self.NameTableOffset = f.readUInt64()
            else:
                self.InfoOffset = f.readUInt32()
                self.NumberOfFiles = f.readUInt32()
                self.NameTableOffset = f.readUInt32()

            self.NamesSize = f.readUInt32()
            if self.Version == self.VERSION_2:
                f.readUInt32()  # Native alignment padding.
                self.BufferSize = f.readUInt64()
                self.ReadBufferSize = self.BufferSize & 0xFFFFFFFF
                self.LoadBufferSize = self.BufferSize >> 32
            else:
                self.ReadBufferSize = f.readUInt32()
                self.LoadBufferSize = f.readUInt32()
                # Preserve the legacy raw uint64 view used by existing callers.
                self.BufferSize = self.ReadBufferSize | (self.LoadBufferSize << 32)
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

                if self.Version == self.VERSION_10:
                    # v10 token: <IQQIIQQ4fB3x> (64 bytes).
                    item.NameOffset = f.readUInt32()
                    item.Unk1 = f.readUInt64()
                    item.PageOffset = f.readUInt64()
                    item.ZSize = f.readUInt32()
                    item.Size = f.readUInt32()
                    item.Unk2 = f.readUInt64()
                    item.Unk3 = f.readUInt64()
                    item.unk4 = f.read(16)
                    item.Comtype = f.readUInt8()
                    item.Tail = f.read(3)
                else:
                    # v1/v9 token: 72 bytes; v2 widens PageOffset to 64 bits.
                    item.NameOffset = f.readUInt32()
                    item.Unk1 = f.readUInt32()
                    item.Unk2 = f.readUInt64()
                    item.PageOffset = f.readUInt64() if self.Version == self.VERSION_2 else f.readUInt32()
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
