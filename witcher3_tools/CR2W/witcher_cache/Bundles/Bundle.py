from collections import OrderedDict
import struct
from .BundleItem import BundleItem
import logging
log = logging.getLogger(__name__)

class InvalidBundleException(Exception):
    pass


def toc_entry_size(header_version):
    try:
        return {3: 320, 5: 304}[header_version]
    except KeyError as exc:
        raise InvalidBundleException(f"Unsupported bundle header version: {header_version}") from exc


class Bundle:
    IDString = b'POTATO70'
    HEADER_SIZE = 32
    ALIGNMENT_TARGET = 4096
    FOOTER_DATA = b"AlignmentUnused"  # Bytes, not string
    TOCEntrySize = 320  # Legacy version 3 debug header.

    def __init__(self, filename=None):
        self.ArchiveAbsolutePath = filename
        self.Items = {}
        self.Patchedfiles = []
        if filename:
            self.Read()

    @property
    def TypeName(self):
        return "Bundle"

    def Read(self):
        self.Items = OrderedDict()

        with open(self.ArchiveAbsolutePath, "rb") as file:
            preamble = file.read(self.HEADER_SIZE)
            if len(preamble) != self.HEADER_SIZE or preamble[:len(self.IDString)] != self.IDString:
                raise InvalidBundleException("Bundle header mismatch.")

            self.header_version = struct.unpack_from("<H", preamble, 20)[0]
            entry_size = toc_entry_size(self.header_version)
            if self.header_version == 5:
                self.bundlesize = struct.unpack_from("<Q", preamble, 8)[0]
                self.dummysize = 0
                self.dataoffset = struct.unpack_from("<I", preamble, 16)[0]
            else:
                self.bundlesize, self.dummysize, self.dataoffset = struct.unpack_from("<III", preamble, 8)
            if self.dataoffset % entry_size:
                raise InvalidBundleException(
                    f"Bundle header size {self.dataoffset} is not divisible by version "
                    f"{self.header_version} entry size {entry_size}."
                )

            file.seek(0x20)

            for _ in range(self.dataoffset // entry_size):
                row = file.read(entry_size)
                if len(row) != entry_size:
                    raise InvalidBundleException("Bundle header ended before its declared size.")

                item = BundleItem()
                item.bundle = self
                item.name = row[:0x100].split(b'\0', 1)[0].decode('iso-8859-1')
                item.hash = row[0x100:0x110]

                if self.header_version == 5:
                    (
                        item.page_offset,
                        item.size,
                        item.zsize,
                        item.crc,
                        item.compression,
                    ) = struct.unpack_from("<QIIIB", row, 0x110)
                    item.empty = 0
                    item.zero = row[0x125:0x130]
                    item.date_string = ""
                else:
                    (
                        item.empty,
                        item.size,
                        item.zsize,
                        item.page_offset,
                        date,
                        time,
                    ) = struct.unpack_from("<6I", row, 0x110)
                    item.zero = row[0x128:0x138]
                    item.crc, item.compression = struct.unpack_from("<2I", row, 0x138)
                    item.date_string = (
                        f"{(date >> 10) & 0x1F}/{(date >> 15) & 0x1F}/{date >> 20} "
                        f"{time >> 22}:{(time >> 16) & 0x3F}:{(time >> 10) & 0x3F}"
                    )

                if item.name not in self.Items:
                    self.Items[item.name] = item
                else:
                    log.warning("Bundle '%s' could not be fully loaded as resource '%s' is defined more than once. Thus, only the first definition was loaded.", self.ArchiveAbsolutePath, item.name)



    @staticmethod
    def Write(Outputpath, rootfolder):
        with open(Outputpath, 'wb') as bw:
            # Write bundle data
            # ... (adapted from C# implementation)
            pass

    @property
    def GetSize(self):
        return self.bundlesize

    # ... Other methods (GetCompressedSize, GetOffset, WriteCompressedData, etc.) similarly adapted

    @staticmethod
    def GetRelativePath(filespec, folder):
        # Convert to relative path
        pass
