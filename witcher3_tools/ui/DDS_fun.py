import struct
import os
from enum import Enum

class EFormat(Enum):
    R8G8B8A8_UNORM = 1
    BC1_UNORM = 2
    # ... (Other formats)

class DDS_PIXELFORMAT:
    def __init__(self):
        self.dwSize = 32
        self.dwFlags = 0
        self.dwFourCC = 0
        self.dwRGBBitCount = 0
        self.dwRBitMask = 0
        self.dwGBitMask = 0
        self.dwBBitMask = 0
        self.dwABitMask = 0

class DDS_HEADER:
    def __init__(self):
        self.dwSize = 124
        self.dwFlags = 0
        # ... (Other header fields)

class DDSUtils:
    DDS_MAGIC = 0x20534444  # "DDS "

    @staticmethod
    def make_fourcc(ch0, ch1, ch2, ch3):
        return (ord(ch0) | ord(ch1) << 8 | ord(ch2) << 16 | ord(ch3) << 24)

    @staticmethod
    def read_header(ddsfile):
        with open(ddsfile, 'rb') as file:
            if os.path.getsize(ddsfile) < 128:
                raise ValueError("File too small to be a valid DDS file")

            magic = struct.unpack('I', file.read(4))[0]
            if magic != DDSUtils.DDS_MAGIC:
                raise ValueError("Not a valid DDS file")

            header_data = file.read(DDS_HEADER.dwSize)
            # ... (Process header_data to extract DDS_HEADER fields)

            return DDS_HEADER()

    @staticmethod
    def calculate_mip_map_size(width, height, format):
        pass
        # ... (Mip map size calculation logic)

    # ... (Other methods)