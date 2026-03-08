from .DDSUtils import DDSUtils

class DDSMetadata:
    def __init__(self, width, height, mipscount=0, format='R8G8B8A8_UNORM', bpp=16, iscubemap=False, slicecount=0, normal=False):
        self.width = width
        self.height = height
        self.mipscount = mipscount
        self.format = format
        self.bpp = bpp
        self.iscubemap = iscubemap
        self.slicecount = slicecount
        self.normal = normal

    @classmethod
    def from_dds_header(cls, ddsheader):
        mask = DDSUtils.DDSCAPS2_CUBEMAP_ALL_FACES & DDSUtils.DDSCAPS2_CUBEMAP
        iscubemap = (ddsheader.dwCaps2 & mask) != 0

        width = ddsheader.dwWidth
        height = ddsheader.dwHeight
        mipscount = ddsheader.dwMipMapCount

        fourcc = ddsheader.ddspf.dwFourCC
        if fourcc == 0x31545844:  # DXT1
            format = 'BC1_UNORM'
        elif fourcc == 0x33545844:  # DXT3
            format = 'BC2_UNORM'
        elif fourcc == 0x35545844:  # DXT5
            format = 'BC3_UNORM'
        elif fourcc == 0x55344342:  # BC4U
            format = 'BC4_UNORM'
        elif fourcc == 0x55354342:  # BC5U
            format = 'BC5_UNORM'
        else:
            format = 'R8G8B8A8_UNORM'

        bpp = 16  # TODO: in vanilla this is always 16 ???
        slicecount = 6 if iscubemap else 0  # TODO: does not account for texarrays
        normal = False  # unused in vanilla

        return cls(width, height, mipscount, format, bpp, iscubemap, slicecount, normal)
