from ..Types.VariousTypes import (CNAME,
                                     CNAME_INDEX,
                                     NAME,
                                     CBufferVLQInt32,
                                     CFloat,
                                     CMatrix4x4,
                                     CPaddedBuffer,
                                     CCompressedBufferTexture,
                                     CUInt16,
                                     CUInt32,
                                     CBytes,
                                     CByteArray)

class CBitmapTexture():
    def __init__(self, CR2WFILE):
        self.__CR2WFILE = CR2WFILE
        self.unk = CUInt32()
        self.MipsCount = CUInt32()
        self.Mipdata = CCompressedBufferTexture()
        self.unk1 = CUInt16()
        self.unk2 = CUInt16()
        self.ResidentmipSize = CUInt32()
        self.Residentmip = CBytes()

    def Create(arg):
        pass

    def Read(self, f, size):
        self.unk.Read(f, 0)
        self.MipsCount.Read(f, 4)
        self.Mipdata.Read(f, size, int(self.MipsCount.val))
        self.ResidentmipSize.Read(f, 4)
        self.unk1.Read(f, 2)
        self.unk2.Read(f, 2)
        self.Residentmip.Read(f, self.ResidentmipSize.val)

    def Write(arg):
        pass

    def WriteJson(arg):
        pass