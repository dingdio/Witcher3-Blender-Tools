from ..bin_helpers import (
                        ReadBit6,
                        ReadVLQInt32,
                        getString,
                        readFloat,
                        readU32,
                        readUShort,)

class CColor:
    def __init__(self):
        self.Red = False
        self.Green = False
        self.Blue = False
        self.Alpha = False

    def Read(self, f):
        self.Red = f.readUInt8()
        self.Green = f.readUInt8()
        self.Blue = f.readUInt8()
        self.Alpha = f.readUInt8()
        return self


#TODO consider merge with STRINGINDEX
class CNAME_INDEX:
    def __init__(self, CR2WFILE):
        self.CR2WFILE = CR2WFILE
        self.value = None
    def Read(self, f, size):
        idx = readUShort(f)
        self.value = self.CR2WFILE.CNAMES[idx]
    def Write():
        pass

class CNAME:
    def __init__(self,f):
        self.value = getString(f)
    def WriteCNAME():
        #GenerateHash
        pass

class NAME:
    def __init__(self, f, CR2WFILE):
        self.stringOffset = readU32(f)
        hashStartOffset = f.tell()
        self.hash = readU32(f)
        f.seek(CR2WFILE.CR2WTable[0].offset + self.stringOffset + CR2WFILE.start)
        # CNAME name;
        self.name = CNAME(f)
        f.seek(hashStartOffset + 4)

class CFloat(object):
    """docstring for CFloat."""
    def __init__(self, CR2WFILE):
        self.val = None
    def Read(self, f, size):
        self.val = readFloat(f)
    def Write():
        pass

class CMatrix4x4(object):
    """docstring for CMatrix4x4."""
    def __init__(self, CR2WFILE):
        super(CMatrix4x4, self).__init__()
        self.ax = None
        self.ay = None
        self.az = None
        self.aw = None
        self.bx = None
        self.by = None
        self.bz = None
        self.bw = None
        self.cx = None
        self.cy = None
        self.cz = None
        self.cw = None
        self.dx = None
        self.dy = None
        self.dz = None
        self.dw = None
    def Read(self, f, size):
        self.ax = readFloat(f)
        self.ay = readFloat(f)
        self.az = readFloat(f)
        self.aw = readFloat(f)
        self.bx = readFloat(f)
        self.by = readFloat(f)
        self.bz = readFloat(f)
        self.bw = readFloat(f)
        self.cx = readFloat(f)
        self.cy = readFloat(f)
        self.cz = readFloat(f)
        self.cw = readFloat(f)
        self.dx = readFloat(f)
        self.dy = readFloat(f)
        self.dz = readFloat(f)
        self.dw = readFloat(f)
    def Write():
        pass
    
class CUInt32(object):
    """docstring for CUInt32."""
    def __init__(self, CR2WFILE):
        super(CUInt32, self).__init__()
        self.val = None
    def Read(self, f, size):
        self.val = readU32(f)
    def Write():
        pass
    
class CUInt16(object):
    """docstring for CUInt16."""
    def __init__(self, CR2WFILE):
        super(CUInt16, self).__init__()
        self.val = None
    def Read(self, f, size):
        self.val = readUShort(f)
    def Write():
        pass

class CPaddedBuffer():
    def __init__(self, CR2WFILE, buffer_type):
        self.buffer_type = buffer_type
        self.CR2WFILE = CR2WFILE
        self.elements = []
        self.padding = 0
    def Read(self, f, size):
        elementcount = ReadBit6(f)
        for _ in range(0, elementcount):
            element = self.buffer_type(self.CR2WFILE)
            element.Read(f, 0)
            self.elements.append(element)
        self.padding = readFloat(f)
    
class CBufferVLQInt32():
    def __init__(self, CR2WFILE, buffer_type, inner_type = False):
        self.buffer_type = buffer_type
        self.inner_type = inner_type
        self.CR2WFILE = CR2WFILE
        self.elements = []
    def Read(self, f, size):
        elementcount = ReadVLQInt32(f)
        for _ in range(0, elementcount):
            if self.inner_type:
                element = self.buffer_type(self.CR2WFILE, self.inner_type)
            else:
                element = self.buffer_type(self.CR2WFILE)
            element.Read(f, 0)
            self.elements.append(element)