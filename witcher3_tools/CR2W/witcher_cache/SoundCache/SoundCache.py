import os
from collections import OrderedDict
from typing import Dict, List, Optional

from ...bStream import bStream
from .SoundCacheItem import SoundCacheItem
from .SoundBanksInfo import SoundBanksInfoXML


class InvalidSoundCacheException(Exception):
    pass


class SoundCache:
    """Parser for Witcher 3 sound cache archives (.cache, magic CS3W)."""

    MAGIC = b"CS3W"
    BIT_LENGTH_32 = 1
    BIT_LENGTH_64 = 2

    def __init__(self, filepath: Optional[str] = None, soundbanks_info: Optional[SoundBanksInfoXML] = None):
        self.ArchiveAbsolutePath = filepath
        self.SoundBanksInfo = soundbanks_info
        self.Version = 0
        self.Unknown1 = 0
        self.Unknown2 = 0
        self.InfoOffset = 0
        self.NumberOfFiles = 0
        self.NameTableOffset = 0
        self.NamesSize = 0
        self.Unk3 = 0
        self.BufferSize = 0
        self.CheckSum = 0

        self.Files: List[SoundCacheItem] = []
        self.Items: Dict[str, SoundCacheItem] = OrderedDict()

        if filepath:
            self._read(filepath)

    @property
    def TypeName(self):
        return "SoundCache"

    def _read(self, filepath: str) -> None:
        with bStream(path=filepath) as stream:
            magic = stream.read(4)
            if magic != self.MAGIC:
                raise InvalidSoundCacheException(
                    f"Invalid sound cache: expected magic {self.MAGIC!r}, got {magic!r}"
                )

            self.Version = stream.readUInt32()
            is_64bit = self.Version >= self.BIT_LENGTH_64

            self.Unknown1 = stream.readUInt32()
            self.Unknown2 = stream.readUInt32()

            if is_64bit:
                self.InfoOffset = stream.readUInt64()
                self.NumberOfFiles = stream.readUInt64()
                self.NameTableOffset = stream.readUInt64()
            else:
                self.InfoOffset = stream.readUInt32()
                self.NumberOfFiles = stream.readUInt32()
                self.NameTableOffset = stream.readUInt32()

            self.NamesSize = stream.readUInt32()
            if is_64bit:
                self.Unk3 = stream.readUInt32()

            self.BufferSize = stream.readUInt64()
            self.CheckSum = stream.readUInt64()

            stream.seek(self.InfoOffset)
            self.Files = []
            self.Items = OrderedDict()

            for _ in range(self.NumberOfFiles):
                item = SoundCacheItem(parent=self)
                if is_64bit:
                    item.NameOffset = stream.readUInt64()
                    item.PageOffset = stream.readUInt64()
                    item.Size = int(stream.readUInt64())
                else:
                    item.NameOffset = stream.readUInt32()
                    item.PageOffset = stream.readUInt32()
                    item.Size = stream.readUInt32()
                item.ZSize = item.Size
                self.Files.append(item)

            for item in self.Files:
                stream.seek(self.NameTableOffset + item.NameOffset)
                raw_name = stream.readStringZero()
                item.RawName = raw_name.replace("/", "\\")
                metadata = self.SoundBanksInfo.lookup(raw_name) if self.SoundBanksInfo is not None else None
                item.Name = (
                    self.SoundBanksInfo.resolve_archive_name(raw_name)
                    if self.SoundBanksInfo is not None
                    else item.RawName
                )
                item.ParentFile = filepath
                if metadata:
                    item.Language = metadata.get("language", "")
                    item.ShortName = metadata.get("short_name", "")
                if item.Name and item.Name not in self.Items:
                    self.Items[item.Name] = item

    def get_item_by_name(self, name: str) -> Optional[SoundCacheItem]:
        return self.Items.get(name, None)

    def __len__(self) -> int:
        return len(self.Files)

    def __iter__(self):
        return iter(self.Files)

    def __repr__(self) -> str:
        return f"SoundCache({self.ArchiveAbsolutePath!r}, {len(self.Files)} files)"

