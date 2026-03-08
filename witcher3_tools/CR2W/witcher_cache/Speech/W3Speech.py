import struct
import os
import mmap
from typing import List
from enum import Enum

from ...bStream import bStream
#from .TextureCacheItem import TextureCacheItem, CommonImageTools, MipmapInfo

from ...bin_helpers import (
                        ReadBit6,
                        ReadVLQInt32,
                        getString,
                        readFloat,
                        readU32,
                        readUShort,
                        readUByte)

from ..blender_common import get_game_path, get_W3_VOICE_PATH
import logging
log = logging.getLogger(__name__)


# Dictionary 'keys' similar to the Lua table
keys = {
    0x83496237: (0x73946816, "pl"),
    0x43975139: (0x79321793, "en"),
    0x75886138: (0x42791159, "de"),
    0x45931894: (0x12375973, "it"),
    0x23863176: (0x75921975, "fr"),
    0x24987354: (0x21793217, "cz"),
    0x18796651: (0x42387566, "es"),
    0x18632176: (0x16875467, "zh"),
    0x77932179: (0x54932186, "ru"),  # 1.0
    0x63481486: (0x42386347, "ru"),  # 1.1
    0x42378932: (0x67823218, "hu"),
    0x54834893: (0x59825646, "jp"),
    0x56328893: (0x43268768, "br"),
    0x56432683: (0x21795135, "tr"),
}

def get_key(key):
    if key == 0:
        return 0, "cleartext"
    elif key in keys:
        return keys[key]
    else:
        raise ValueError(f"\n\n!!! unknown key '0x{key:08X}' !!!\n")

def pad_filename(filename):
    name =filename
    try:
        # Assuming the filename is a number, pad it with zeros
        padded_name = f"{int(name):010}"
    except ValueError:
        # If the filename is not a number, return it unchanged
        return filename

    return padded_name


class EBundleType(Enum):
    ANY = 1
    BUNDLE = 2
    COLLISIONCACHE = 3
    TEXTURECACHE = 4
    SOUNDCACHE = 5
    SPEECH = 6
    SHADER = 7

class SpeechEntry:
    def __init__(self, bundle=None, id=None, id_high=None, wem_offs=None, wem_size=None, cr2w_offs=None, cr2w_size=None, duration=None):
        self.bundle = bundle
        self.id = id
        self.id_high = id_high
        self.wem_offs = wem_offs
        self.wem_size = wem_size
        self.cr2w_offs = cr2w_offs
        self.cr2w_size = cr2w_size
        self.duration = duration

        if bundle is not None and id is not None and id_high is not None:
            self.size = wem_size + cr2w_size
            self.z_size = wem_size + cr2w_size
            self.name = id #str(id) + ".cr2w_wem_pair"
            self.page_offset = cr2w_offs
        else:
            self.size = self.z_size = self.name = self.page_offset = None

        self.compression_type = "None"

    def extract(self, output):
        with open(self.bundle.ArchiveAbsolutePath, 'rb') as file:
            with mmap.mmap(file.fileno(), length=0, access=mmap.ACCESS_READ) as mm:
                mm.seek(self.page_offset)
                output.write(mm.read(self.z_size))

    def extract_to_file(self, file_name):
        base_path = get_W3_VOICE_PATH()
        base_file_name = os.path.splitext(os.path.basename(file_name))[0]
        base_file_name = pad_filename(base_file_name)


        self.page_offset = self.cr2w_offs
        self.z_size = self.cr2w_size
        cr2w_file_path = os.path.join(base_path, base_file_name + ".cr2w")
        with open(cr2w_file_path, 'wb') as file:
            self.extract(file)

        self.page_offset = self.wem_offs
        self.z_size = self.wem_size
        wem_file_path = os.path.join(base_path, base_file_name + ".wem")
        with open(wem_file_path, 'wb') as file:
            self.extract(file)

        return wem_file_path

class W3Speech(object):
    _MagicInt = 1465077827

    def __init__(self, filePath = None, id='', version=163, language_key=None, item_infos=[]):
        super(W3Speech, self).__init__()
        self.filePath = filePath
        # self.Files:List[TextureCacheItem] = []
        # self.Names:List[str] = []
        # self.MipOffsets:List[int] = []
        
        #footer
        # self.crc: int = 0
        # self.used_pages: int = 0
        # self.entry_count: int = 0
        # self.string_table_size: int = 0
        # self.mip_table_entry_count: int = 0
        # self.magic = b'CPSW'
        # self.version: int = 6
        
        self.ArchiveAbsolutePath = filePath
        self.id = id #public String id { get; set; }
        self.version = version #public UInt32 version { get; set; } # Usually 163 or 162.
        self.language_key = language_key #public W3LanguageKey language_key { get; set; }
        self.item_infos:List[SpeechEntry] = item_infos # public IEnumerable<SpeechEntry> item_infos { get; set; }
        
        if filePath:
            self.read(filePath)

    @property
    def type_name(self) -> EBundleType:
        return EBundleType.SPEECH
    
    def read(self, filepath):
        file:bStream = bStream(path=filepath)
        file_str = file.read(4).decode('utf-8')
        version = file.readUInt32()
        key1 = file.readUInt16()
        item_count_value, item_count_len = ReadBit6(file.fhandle, True)

        raw_item_infos = []
        for _ in range(item_count_value):
            lang_specific_ID = file.readUInt32()
            id_high = file.readUInt32()
            wave_offs = file.readUInt32() + 4
            file.readUInt32()  # Skip 4 bytes
            wave_size = file.readUInt32() - 12
            file.readUInt32()  # Skip 4 bytes
            cr2w_offs = file.readUInt32()
            file.readUInt32()  # Skip 4 bytes
            cr2w_size = file.readUInt32()
            file.readUInt32()  # Skip 4 bytes

            raw_item_infos.append((lang_specific_ID, id_high, wave_offs, wave_size, cr2w_offs, cr2w_size))
        key2 = file.readUInt16()
        key = (key1 << 16) | key2

        log.debug("key: 0x%08X", key)
        magic, lang = get_key(key)
        log.debug("-> magic: 0x%08X (%s)", magic, lang)
        
        position = 4 + 4 + 2 + item_count_len + item_count_value * 10 * 4 + 2

        item_infos = []
        for item in sorted(raw_item_infos, key=lambda x: x[2]):
            lang_id = item[0] ^ magic # convert id
            duration_offset = item[2] - position + item[3]
            file.seek(duration_offset, os.SEEK_CUR) 
            position += duration_offset
            duration = file.readFloat()  # Assuming readFloat is implemented in bStream
            position += 4
            item_infos.append(SpeechEntry(self, lang_id, item[1], item[2], item[3], item[4], item[5], duration))

        self.ArchiveAbsolutePath = filepath
        self.file_str = str #public String id { get; set; }
        self.version = version #public UInt32 version { get; set; } # Usually 163 or 162.
        self.language_key = key #public W3LanguageKey language_key { get; set; }
        self.item_infos:List[SpeechEntry] = item_infos # public IEnumerable<SpeechEntry> item_infos { get; set; }
        
class WemCr2wInputPair:
    def __init__(self, id, id_high, wem, wem_size, duration, cr2w, cr2w_size):
        self._id = id
        self._id_high = id_high
        self._wem = wem
        self._wem_size = wem_size
        self._duration = duration
        self._cr2w = cr2w
        self._cr2w_size = cr2w_size

    @property
    def id(self):
        return self._id

    @property
    def id_high(self):
        return self._id_high

    @property
    def wem(self):
        return self._wem

    @property
    def wem_size(self):
        return self._wem_size

    @property
    def duration(self):
        return self._duration

    @property
    def cr2w(self):
        return self._cr2w

    @property
    def cr2w_size(self):
        return self._cr2w_size

    def __str__(self):
        return f"WemCr2wInputPair({self.id},{self.id_high},{self.wem},{self.wem_size},{self.duration},{self.cr2w},{self.cr2w_size})"
