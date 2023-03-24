from typing import List
import os
import numpy as np
from ..bStream import *
from ..bin_helpers import ReadBit6, FileSize
from .W3Language import W3Language, languages
from .W3StringBlock1 import *
from .W3StringBlock2 import *

class W3StringFile(object):
    def __init__(self):

        self._IDString:bytes = b'RTSW'
        self._block1count: int
        self._block2count: int
        self._block3count: int
        self._key1: np.ushort
        self._key2: np.ushort
        self._language: W3Language
        self._version: np.uint
        self.block1: List[W3StringBlock1] = []
        self.block2: List[W3StringBlock2] = []
        self.block1Unsorted: List[W3StringBlock1] = []
        self.Incomplete: bool = False

    def Create(self):
        pass
    
    def Read(self, stream:bStream):

        filetype = stream.read(4)
        if filetype != self._IDString:
            raise Exception("Invalid file format")
        version = stream.readUInt32()

        #629299
        key1 = stream.readUInt16()
        stream.seek(-2, os.SEEK_END)
        key2 = stream.readUInt16()
        key = (key1 << 16 | key2)
        
        language = next((lang for lang in languages if lang.Key.value == key), None)
        
        stream.seek(10, os.SEEK_SET)
        # Read block 1
        # str_id and actual string
        block1count = ReadBit6(stream.fhandle)
        block1: List[W3StringBlock1] = []
        for _ in range(block1count):
            newblock = W3StringBlock1(stream, language.Magic.value)
            block1.append(newblock)
            
        block2count = ReadBit6(stream.fhandle)
        block2: List[W3StringBlock2] = []
        for _ in range(block2count):
            newblock = W3StringBlock2(stream, language.Magic.value)
            block2.append(newblock)
            

        block3count = ReadBit6(stream.fhandle)
        str_start = stream.tell()
        
        for block in block1:
            offset = block.offset * 2 + str_start

            stream.seek(offset, os.SEEK_SET)

            string_key = (language.Magic.value >> 8) & 0xffff
            #for (var i = 0; i < block.strlen; i++)
            for _ in range(block.strlen):
                b1 = stream.readUByte()
                b2 = stream.readUByte()

                char_key = (((block.strlen + 1) * string_key) & 0xffff)

                b1 = (b1 ^ ((char_key >> 0) & 0xff))
                b2 = (b2 ^ ((char_key >> 8) & 0xff))

                string_key = (((string_key << 1) | (string_key >> 15)) & 0xffff)

                block.str += chr(b1 + (b2 << 8))
                
        strbuffsize = (FileSize(stream.fhandle) - 2) - str_start
        stream.seek(int(block3count * 2 + str_start), os.SEEK_SET)
        left = FileSize(stream.fhandle) - stream.tell() - 2
        if (left > 0):
            self.Incomplete = True
            
            
        self.block1 = block1
        self.block2 = block2
        #self.block1Unsorted = block1.copy()
        self.block1.sort(key=lambda x: x.str_id_hashed)
        self.block2.sort(key=lambda x: x.str_key_hex)

    def Write(self, stream):
        pass
    