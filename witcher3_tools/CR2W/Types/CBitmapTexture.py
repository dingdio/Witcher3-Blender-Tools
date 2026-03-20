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
        self.Mipdata = CCompressedBufferTexture(CR2WFILE)
        self.unk1 = CUInt16() #colour bit?
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
        remaining = None
        try:
            export = self.__CR2WFILE.CR2WExport[self.__CR2WFILE.currentExport]
            export_end = self.__CR2WFILE.start + export.dataOffset + export.dataSize
            remaining = max(0, export_end - f.tell())
        except Exception:
            pass

        if remaining is None or remaining >= 4:
            self.unk1.Read(f, 2)
            self.unk2.Read(f, 2)
            if remaining is not None:
                remaining = max(0, remaining - 4)
        else:
            self.unk1.val = 0
            self.unk2.val = 0

        resident_size = int(self.ResidentmipSize.val or 0)
        if remaining is not None:
            resident_size = min(resident_size, remaining)
        self.Residentmip.Read(f, resident_size)

    def Write(arg):
        pass

    def WriteJson(arg):
        pass
