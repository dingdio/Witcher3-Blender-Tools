import struct
import os
from ...bStream import bStream
from .DDS_Enums import D3D10_RESOURCE_DIMENSION, DXGI_FORMAT, EFormat

class DDS_PIXELFORMAT:
    def __init__(self,
                dwSize,
                dwFlags,
                dwFourCC,
                dwRGBBitCount,
                dwRBitMask,
                dwGBitMask,
                dwBBitMask,
                dwABitMask):
        self.dwSize: int = dwSize #uint
        self.dwFlags: int = dwFlags #uint
        self.dwFourCC: int = dwFourCC #uint
        self.dwRGBBitCount: int = dwRGBBitCount #uint
        self.dwRBitMask: int = dwRBitMask #uint
        self.dwGBitMask: int = dwGBitMask #uint
        self.dwBBitMask: int = dwBBitMask #uint
        self.dwABitMask: int = dwABitMask #uint

class DDS_HEADER:
    def __init__(self,
                dwSize,
                dwFlags,
                dwHeight,
                dwWidth,
                dwPitchOrLinearSize,
                dwDepth,
                dwMipMapCount,
                dwReserved1,
                dwReserved2,
                dwReserved3,
                dwReserved4,
                dwReserved5,
                dwReserved6,
                dwReserved7,
                dwReserved8,
                dwReserved9,
                dwReserved10,
                dwReserved11,
                ddspf,
                dwCaps,
                dwCaps2,
                dwCaps3,
                dwCaps4,
                dwReserved12):
        self.dwSize: int = dwSize  # uint
        self.dwFlags: int = dwFlags  # uint
        self.dwHeight: int = dwHeight  # uint
        self.dwWidth: int = dwWidth  # uint
        self.dwPitchOrLinearSize: int = dwPitchOrLinearSize  # uint
        self.dwDepth: int = dwDepth  # uint
        self.dwMipMapCount: int = dwMipMapCount  # uint
        self.dwReserved1: int = dwReserved1  # uint
        self.dwReserved2: int = dwReserved2  # uint
        self.dwReserved3: int = dwReserved3  # uint
        self.dwReserved4: int = dwReserved4  # uint
        self.dwReserved5: int = dwReserved5  # uint
        self.dwReserved6: int = dwReserved6  # uint
        self.dwReserved7: int = dwReserved7  # uint
        self.dwReserved8: int = dwReserved8  # uint
        self.dwReserved9: int = dwReserved9  # uint
        self.dwReserved10: int = dwReserved10  # uint
        self.dwReserved11: int = dwReserved11  # uint
        self.ddspf: DDS_PIXELFORMAT = ddspf 
        self.dwCaps: int = dwCaps  # uint
        self.dwCaps2: int = dwCaps2  # uint
        self.dwCaps3: int = dwCaps3  # uint
        self.dwCaps4: int = dwCaps4  # uint
        self.dwReserved12: int = dwReserved12  # uint


class DDS_HEADER_DXT10:
    def __init__(self,
                dxgiFormat,
                resourceDimension,
                miscFlag,
                arraySize,
                miscFlags2):
        self.dxgiFormat: DXGI_FORMAT = dxgiFormat
        self.resourceDimension: D3D10_RESOURCE_DIMENSION = resourceDimension
        self.miscFlag:int = miscFlag
        self.arraySize:int = arraySize
        self.miscFlags2:int = miscFlags2

class DDSUtils:
    # DDS_HEADER constants
    DDS_MAGIC = 0x20534444  # "DDS "
    HEADER_SIZE = 124

    # dwFlags
    DDSD_CAPS = 0x00000001          # required
    DDSD_HEIGHT = 0x00000002        # required
    DDSD_WIDTH = 0x00000004         # required
    DDSD_PITCH = 0x00000008
    DDSD_PIXELFORMAT = 0x00001000   # required
    DDSD_MIPMAPCOUNT = 0x00020000
    DDSD_LINEARSIZE = 0x00080000
    DDSD_DEPTH = 0x00800000

    DDS_HEADER_FLAGS_TEXTURE = 0x00001007  # DDSD_CAPS | DDSD_HEIGHT | DDSD_WIDTH | DDSD_PIXELFORMAT 

    # dwCaps
    DDSCAPS_COMPLEX = 0x00000008
    DDSCAPS_MIPMAP = 0x00400000
    DDSCAPS_TEXTURE = 0x00001000

    # dwCaps2
    DDSCAPS2_CUBEMAP = 0x00000200
    DDS_CUBEMAP_POSITIVEX = 0x00000600  # DDSCAPS2_CUBEMAP | DDSCAPS2_CUBEMAP_POSITIVEX
    DDS_CUBEMAP_NEGATIVEX = 0x00000a00  # DDSCAPS2_CUBEMAP | DDSCAPS2_CUBEMAP_NEGATIVEX
    DDS_CUBEMAP_POSITIVEY = 0x00001200  # DDSCAPS2_CUBEMAP | DDSCAPS2_CUBEMAP_POSITIVEY
    DDS_CUBEMAP_NEGATIVEY = 0x00002200  # DDSCAPS2_CUBEMAP | DDSCAPS2_CUBEMAP_NEGATIVEY
    DDS_CUBEMAP_POSITIVEZ = 0x00004200  # DDSCAPS2_CUBEMAP | DDSCAPS2_CUBEMAP_POSITIVEZ
    DDS_CUBEMAP_NEGATIVEZ = 0x00008200  # DDSCAPS2_CUBEMAP | DDSCAPS2_CUBEMAP_NEGATIVEZ
    DDSCAPS2_CUBEMAP_ALL_FACES = (DDS_CUBEMAP_POSITIVEX | DDS_CUBEMAP_NEGATIVEX |
                                DDS_CUBEMAP_POSITIVEY | DDS_CUBEMAP_NEGATIVEY |
                                DDS_CUBEMAP_POSITIVEZ | DDS_CUBEMAP_NEGATIVEZ)
    DDSCAPS2_VOLUME = 0x00200000

    # DDS_HEADER_DXT10
    D3D10_RESOURCE_MISC_GENERATE_MIPS = 0x00000001
    DDS_RESOURCE_MISC_TEXTURECUBE = 0x00000004
    DDS_ALPHA_MODE_UNKNOWN = 0x00000000


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


    #!region DDS_PIXELFORMAT
    # dwSize
    PIXELFORMAT_SIZE = 32
    # dwFlags
    DDPF_ALPHAPIXELS = 0x00000001
    DDPF_ALPHA = 0x00000002
    DDPF_FOURCC = 0x00000004
    DDPF_RGB = 0x00000040
    DDPF_NORMAL = 0x80000000 # Custom nv flag

    #dwRGBBitCount     dwRBitMask      dwGBitMask      dwBBitMask      dwABitMask
    def DDSPF_A8R8G8B8(): return [32, 0x00ff0000, 0x0000ff00, 0x000000ff, 0xff000000]
    def DDSPF_X8R8G8B8(): return [32, 0x00ff0000, 0x0000ff00, 0x000000ff, 0x00000000]
    def DDSPF_A8B8G8R8(): return [32, 0x000000ff, 0x0000ff00, 0x00ff0000, 0xff000000]
    def DDSPF_X8B8G8R8(): return [32, 0x000000ff, 0x0000ff00, 0x00ff0000, 0x00000000]
    def DDSPF_G16R16():   return [32, 0x0000ffff, 0xffff0000, 0x00000000, 0x00000000]
    def DDSPF_R5G6B5():   return [16, 0x0000f800, 0x000007e0, 0x0000001f, 0x00000000]
    def DDSPF_A1R5G5B5(): return [16, 0x00007c00, 0x000003e0, 0x0000001f, 0x00000000]
    def DDSPF_A4R4G4B4(): return [16, 0x00000f00, 0x000000f0, 0x0000000f, 0x0000f000]
    def DDSPF_R8G8B8():   return [24, 0x00ff0000, 0x0000ff00, 0x000000ff, 0x00000000]

    #!endregion


    #!region !WRITING
    
    @staticmethod
    def GenerateHeader(metadata):
        height = metadata.height
        width = metadata.width
        mipscount = metadata.mipscount
        iscubemap = metadata.iscubemap
        format = metadata.format
        dxt10 = False
        
        ddspf = DDS_PIXELFORMAT(
            dwSize = DDSUtils.PIXELFORMAT_SIZE,
            dwFlags = 0,
            dwFourCC = 0,
            dwRGBBitCount = 0,
            dwRBitMask = 0,
            dwGBitMask = 0,
            dwBBitMask = 0,
            dwABitMask = 0
        )

        header = DDS_HEADER(
            dwSize = DDSUtils.HEADER_SIZE,
            dwFlags = DDSUtils.DDS_HEADER_FLAGS_TEXTURE,
            dwHeight = height,
            dwWidth = width,
            dwPitchOrLinearSize = 0,
            dwDepth = 0,
            dwMipMapCount = 0,
            dwReserved1 = 0,
            dwReserved2 = 0,
            dwReserved3 = 0,
            dwReserved4 = 0,
            dwReserved5 = 0,
            dwReserved6 = 0,
            dwReserved7 = 0,
            dwReserved8 = 0,
            dwReserved9 = 0,
            dwReserved10 = 0,
            dwReserved11 = 0,
            ddspf = ddspf,
            dwCaps = DDSUtils.DDSCAPS_TEXTURE,
            dwCaps2 = 0,
            dwCaps3 = 0,
            dwCaps4 = 0,
            dwReserved12 = 0,
        )

        dxt10header = DDS_HEADER_DXT10(
            dxgiFormat = 0,
            resourceDimension = D3D10_RESOURCE_DIMENSION.D3D10_RESOURCE_DIMENSION_TEXTURE2D,
            miscFlag = 0,
            arraySize = metadata.slicecount,
            miscFlags2 = 0
        )

        if (mipscount > 0):
            header.dwMipMapCount = mipscount

        def SetPixelmask(pfmtfactory, pfmt):
            masks = pfmtfactory()
            pfmt.dwRGBBitCount = masks[0]
            pfmt.dwRBitMask = masks[1]
            pfmt.dwGBitMask = masks[2]
            pfmt.dwBBitMask = masks[3]
            pfmt.dwABitMask = masks[4]


        # Set pixel format
        if metadata.format == EFormat.R8G8B8A8_UNORM:
            SetPixelmask(DDSUtils.DDSPF_A8R8G8B8, ddspf)
        elif metadata.format == EFormat.BC1_UNORM:
            ddspf.dwFourCC = struct.unpack('<I', b'DXT1')[0]
        elif metadata.format == EFormat.BC2_UNORM:
            ddspf.dwFourCC = struct.unpack('<I', b'DXT3')[0]
        elif metadata.format == EFormat.BC3_UNORM:
            ddspf.dwFourCC = struct.unpack('<I', b'DXT5')[0]
        elif metadata.format == EFormat.BC4_UNORM:
            ddspf.dwFourCC = struct.unpack('<I', b'BC4U')[0]
        elif metadata.format == EFormat.BC5_UNORM:
            ddspf.dwFourCC = struct.unpack('<I', b'BC5U')[0]
        elif metadata.format == EFormat.BC7_UNORM:
            dxt10 = True
        else:
            raise Exception("Missing Format")

        if dxt10:
            ddspf.dwFourCC = struct.unpack('<I', b'DX10')[0]

        # Set other flags
        if ddspf.dwABitMask != 0:
            ddspf.dwFlags |= DDSUtils.DDPF_ALPHAPIXELS
        if ddspf.dwFourCC != 0:
            ddspf.dwFlags |= DDSUtils.DDPF_FOURCC
        if ddspf.dwRGBBitCount != 0 and (ddspf.dwRBitMask != 0 or ddspf.dwGBitMask != 0 or ddspf.dwBBitMask != 0):
            ddspf.dwFlags |= DDSUtils.DDPF_RGB
        if metadata.normal:
            ddspf.dwFlags |= DDSUtils.DDPF_NORMAL
        #Set pixel format END
        
        #!dwPitchOrLinearSize
        p = 0
        if format == EFormat.R8G8B8A8_UNORM:
            bpp = ddspf.dwRGBBitCount
            header.dwPitchOrLinearSize = (width * bpp + 7) // 8
            header.dwFlags |= DDSUtils.DDSD_PITCH
        elif format in (EFormat.BC1_UNORM, EFormat.BC4_UNORM):
            p = width * height // 2  # max(1, width // 4) * max(1, height // 4) * 8
            header.dwPitchOrLinearSize = p
            header.dwFlags |= DDSUtils.DDSD_LINEARSIZE
        elif format in (EFormat.BC2_UNORM, EFormat.BC3_UNORM, EFormat.BC5_UNORM, EFormat.BC7_UNORM):
            p = width * height  # max(1, width // 4) * max(1, height // 4) * 16
            header.dwPitchOrLinearSize = p
            header.dwFlags |= DDSUtils.DDSD_LINEARSIZE
        else:
            raise ValueError("Missing Format: {}".format(format))

        # Caps
        if metadata.iscubemap or metadata.mipscount > 0:
            header.dwCaps |= DDSUtils.DDSCAPS_COMPLEX
        if metadata.mipscount > 0:
            header.dwCaps |= DDSUtils.DDSCAPS_MIPMAP

        # Caps2
        if metadata.iscubemap:
            header.dwCaps2 |= DDSUtils.DDSCAPS2_CUBEMAP_ALL_FACES | DDSUtils.DDSCAPS2_CUBEMAP

        # Flags
        if metadata.mipscount > 0:
            header.dwFlags |= DDSUtils.DDSD_MIPMAPCOUNT

        # DXT10
        if dxt10:
            # dxgiFormat
            if metadata.format == EFormat.BC7_UNORM:
                dxt10header.dxgiFormat = DXGI_FORMAT.DXGI_FORMAT_BC7_UNORM
            else:
                raise Exception("Missing Format: {}".format(metadata.format))
            
            # Resource dimension and misc flags
            if metadata.iscubemap:
                dxt10header.miscFlag |= DDSUtils.DDS_RESOURCE_MISC_TEXTURECUBE
            if metadata.mipscount > 0:
                dxt10header.miscFlag |= DDSUtils.D3D10_RESOURCE_MISC_GENERATE_MIPS

            # Array size
            if metadata.iscubemap:
                dxt10header.arraySize = metadata.slicecount

        return header, dxt10header
    
    
    @staticmethod
    def GenerateAndWriteHeader(stream:bStream, metadata):
        (header, dxt10header) = DDSUtils.GenerateHeader(metadata)
        stream.write(struct.pack('<I', DDSUtils.DDS_MAGIC))
        stream.write(struct.pack('<IIIIIIIIIIIIIIIIIIIIIIIIIIIIIII',
                    header.dwSize,
                    header.dwFlags,
                    header.dwHeight,
                    header.dwWidth,
                    header.dwPitchOrLinearSize,
                    header.dwDepth,
                    header.dwMipMapCount,
                    header.dwReserved1,
                    header.dwReserved2,
                    header.dwReserved3,
                    header.dwReserved4,
                    header.dwReserved5,
                    header.dwReserved6,
                    header.dwReserved7,
                    header.dwReserved8,
                    header.dwReserved9,
                    header.dwReserved10,
                    header.dwReserved11,
                    
                    #header.ddspf,
                    header.ddspf.dwSize,
                    header.ddspf.dwFlags,
                    header.ddspf.dwFourCC,
                    header.ddspf.dwRGBBitCount,
                    header.ddspf.dwRBitMask,
                    header.ddspf.dwGBitMask,
                    header.ddspf.dwBBitMask,
                    header.ddspf.dwABitMask,
                    
                    header.dwCaps,
                    header.dwCaps2,
                    header.dwCaps3,
                    header.dwCaps4,
                    header.dwReserved12))
        if header.ddspf.dwFourCC == struct.unpack('<I', b'DX10')[0]:
            stream.write(struct.pack('<IIIII',
                        dxt10header.dxgiFormat.value,
                        dxt10header.resourceDimension.value,
                        dxt10header.miscFlag,
                        dxt10header.arraySize,
                        dxt10header.miscFlags2))
            