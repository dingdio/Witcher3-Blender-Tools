import struct
import os
from typing import List
from enum import Enum

from ...bStream import bStream
from .TextureCacheItem import TextureCacheItem, CommonImageTools, MipmapInfo

class EBundleType(Enum):
    ANY = 1
    BUNDLE = 2
    COLLISIONCACHE = 3
    TEXTURECACHE = 4
    SOUNDCACHE = 5
    SPEECH = 6
    SHADER = 7

class TextureCache(object):
    _MagicInt = 1415070536

    def __init__(self, filePath):
        super(TextureCache, self).__init__()
        self.filePath = filePath
        self.Files:List[TextureCacheItem] = []
        self.Names:List[str] = []
        self.MipOffsets:List[int] = []
        
        #footer
        self.crc: int = 0
        self.used_pages: int = 0
        self.entry_count: int = 0
        self.string_table_size: int = 0
        self.mip_table_entry_count: int = 0
        self.magic = b'HCXT'
        self.version: int = 6
        
        self.read(filePath)

    @property
    def type_name(self) -> EBundleType:
        return EBundleType.TEXTURECACHE

    def read(self, filepath):
        #try:
        if True:
            archive_absolute_path = filepath

            stream:bStream = bStream(path = archive_absolute_path)
            stream.decoder = 'ISO-8859-1'

            mip_offsets = []
            files = []
            names = []

            with stream.fhandle as br:
                # Reading the footer
                br.seek(-32, os.SEEK_END)
                crc, used_pages, entry_count, string_table_size, mip_table_entry_count, magic, version = struct.unpack('QIIIIII', br.read(32))

                self.crc: int = crc
                self.used_pages: int = used_pages
                self.entry_count: int = entry_count
                self.string_table_size: int = string_table_size
                self.mip_table_entry_count: int = mip_table_entry_count
                self.magic = magic
                self.version: int = version
                
                if magic != self._MagicInt:
                    raise Exception("Invalid file!")

                # Calculating the string table offset
                string_table_offset = -(32 + (entry_count * 52) + string_table_size + (mip_table_entry_count * 4))
                br.seek(string_table_offset, os.SEEK_END)

                # MipMapTable
                for _ in range(mip_table_entry_count):
                    mip_offsets.append(struct.unpack('I', br.read(4))[0])

                # StringTable
                for _ in range(entry_count):
                    names.append(stream.readString(0, True))

                # EntryTable
                entry_table_offset = -(32 + (entry_count * 52))
                br.seek(entry_table_offset, os.SEEK_END)

                for i in range(entry_count):
                    ti = TextureCacheItem(self)
                    ti.Name = names[i]
                    ti.ParentFile = archive_absolute_path

                    ti.Hash = stream.readUInt32()  # Replace with your method to read an unsigned int 32
                    ti.StringTableOffset = stream.readInt32()  # Replace with your method to read an int 32
                    ti.PageOffset = stream.readUInt32()
                    ti.CompressedSize = stream.readUInt32()
                    ti.UncompressedSize = stream.readUInt32()

                    ti.BaseAlignment = stream.readUInt32()
                    ti.BaseWidth = stream.readInt16() 
                    ti.BaseHeight = stream.readInt16()
                    ti.Mipcount = stream.readInt16()
                    ti.SliceCount = stream.readInt16()

                    ti.MipOffsetIndex = stream.readInt32()
                    ti.NumMipOffsets = stream.readInt32()
                    ti.TimeStamp = stream.readInt64() 

                    ti.Type1 = stream.readUByte()
                    ti.Type2 = stream.readUByte()
                    ti.IsCube = stream.readUByte()
                    ti.Unk1 = stream.readUByte()

                    ti.Format = CommonImageTools.get_eformat_from_redengine_byte(ti.Type1)
                    files.append(ti)

                for t in files:
                    t:TextureCacheItem
                    br.seek(t.PageOffset * 4096, os.SEEK_SET)
                    t.ZSize = stream.readUInt32()  # Compressed size
                    t.Size = stream.readUInt32()  # Uncompressed size
                    t.MipIdx = stream.readUByte()  # maybe the 48bit part of OFFSET

                    lastpos = br.tell() + t.ZSize

                    for i in range(t.NumMipOffsets):
                        br.seek(lastpos)

                        mzsize = stream.readUInt32()
                        msize = stream.readUInt32()
                        midx = stream.readUByte()

                        t.MipMapInfo.append(MipmapInfo(lastpos + 9, mzsize, msize, midx))

                        lastpos += 9 + mzsize
            
            stream.close()
            self.MipOffsets = mip_offsets 
            self.Files = files
            self.Names = names

        # except Exception as e:
        #     print(f"Error: {e}")
        #     self.MipOffsets = mip_offsets 
        #     self.Files = files
        #     self.Names = names