from collections import OrderedDict
import os
import struct
from io import BytesIO
from ...bStream import bStream
from .BundleItem import BundleItem
import logging
log = logging.getLogger(__name__)

class InvalidBundleException(Exception):
    pass

class Bundle:
    IDString = b'POTATO70'
    HEADER_SIZE = 32
    ALIGNMENT_TARGET = 4096
    FOOTER_DATA = b"AlignmentUnused"  # Bytes, not string
    TOCEntrySize = 0x100 + 16 + 4 + 4 + 4 + 4 + 8 + 16 + 4 + 4

    def __init__(self, filename=None):
        self.ArchiveAbsolutePath = filename
        self.Items = {}
        self.Patchedfiles = []
        if filename:
            self.Read()

    @property
    def TypeName(self):
        return "Bundle"

    # def Read(self):
    #     with open(self.ArchiveAbsolutePath, 'rb') as reader:
    #         idstring = reader.read(len(self.IDString))
    #         if idstring != self.IDString:
    #             raise Exception("Bundle header mismatch.")

    #         self.bundlesize, self.dummysize, self.dataoffset = struct.unpack('III', reader.read(12))
    #         reader.seek(0x20)

    #         while reader.tell() < self.dataoffset + 0x20:
    #             # Read and process TOC entry
    #             # ... (similar to C# implementation, adjusted for Python)
    #             pass
    def Read(self):
        self.Items = OrderedDict()

        with bStream(path=self.ArchiveAbsolutePath) as file:
            idstring = file.read(len(self.IDString))

            if idstring != self.IDString:
                raise InvalidBundleException("Bundle header mismatch.")

            self.bundlesize = file.readUInt32()
            self.dummysize = file.readUInt32()
            self.dataoffset = file.readUInt32()

            file.seek(0x20)

            while file.tell() < self.dataoffset + 0x20:
                item = BundleItem()
                item.bundle = self

                strname = file.read(0x100).decode('iso-8859-1')
                item.name = strname.split('\0', 1)[0]
                item.hash = file.read(16)
                item.empty = file.readUInt32()
                item.size = file.readUInt32()
                item.zsize = file.readUInt32()
                item.page_offset = file.readUInt32()

                date = file.readUInt32()
                y = date >> 20
                m = (date >> 15) & 0x1F
                d = (date >> 10) & 0x1F

                time = file.readUInt32()
                h = time >> 22
                n = (time >> 16) & 0x3F
                s = (time >> 10) & 0x3F

                item.date_string = f"{d}/{m}/{y} {h}:{n}:{s}"

                item.zero = file.read(16)  # always zero
                item.crc = file.readUInt32()
                item.compression = file.readUInt32()

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
