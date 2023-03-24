from ..bin_helpers import (
                        ReadBit6,
                        ReadVLQInt32,
                        getString,
                        readFloat,
                        readU32,
                        readUShort,
                        readUByte)

class CColor:
    def __init__(self):
        self.Red = False
        self.Green = False
        self.Blue = False
        self.Alpha = False

    def Set(self,r,g,b,a):
        self.Red = r
        self.Green = g
        self.Blue = b
        self.Alpha = a
        return self

    def Read(self, f):
        self.Red = readUByte(f)
        self.Green = readUByte(f)
        self.Blue = readUByte(f)
        self.Alpha = readUByte(f)
        return self


class CNAME:
    def __init__(self, f = None, **kwargs):
        if f and not kwargs:
            self.value = getString(f)
        else:
            self.Create(kwargs)

    def Create(self, args):
        for arg in args.items():
            setattr(self, arg[0], arg[1])

    def WriteCNAME():
        #GenerateHash
        pass

class NAME:
    def __init__(self, f = None, CR2WFILE = None, name = None):
        self.__CR2WFILE = CR2WFILE
        if f and not name:
            self.Read(f, CR2WFILE)
        else:
            self.Create(name)

    def Read(self, f, CR2WFILE):
        self.stringOffset = readU32(f)
        hashStartOffset = f.tell()
        self.hash = readU32(f)
        f.seek(CR2WFILE.CR2WTable[0].offset + self.stringOffset + CR2WFILE.start)
        # CNAME name;
        self.name = CNAME(f)
        f.seek(hashStartOffset + 4)

    def Create(self, name:str):
        self.hash = 123
        self.name = CNAME( value = name )

#TODO consider merge with STRINGINDEX
class CNAME_INDEX:
    def __init__(self, CR2WFILE, value = None):
        self.CR2WFILE = CR2WFILE
        if value:
            self.Create(value)
        else:
            self.value = None
            self.index = None

    def Read(self, f, size):
        self.index = readUShort(f)
        self.value = self.CR2WFILE.CNAMES[self.index]

    def Create(self, value:NAME):
        self.CR2WFILE.CNAMES.append(value)
        self.index = len(self.CR2WFILE.CNAMES) - 1
        self.value = value

    def Write():
        pass

class CFloat(object):
    """docstring for CFloat."""
    def __init__(self, CR2WFILE, val = None):
        self.val = val
        self.type = 'Float'
        self.theType = 'CFloat'
    def Read(self, f, size):
        self.val = readFloat(f)
    def Write():
        pass

class CMatrix4x4(object):
    """docstring for CMatrix4x4."""
    def __init__(self, CR2WFILE):
        self.theType = 'CMatrix4x4'
        super(CMatrix4x4, self).__init__()
        self.fields = []
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
    def Create(self, *args):
        self.fieldNames = [
            "ax",
            "ay",
            "az",
            "aw",
            "bx",
            "by",
            "bz",
            "bw",
            "cx",
            "cy",
            "cz",
            "cw",
            "dx",
            "dy",
            "dz",
            "dw",
        ]
        if args:
            for idx, value in enumerate(self.fieldNames):
                setattr(self, value, float(args[idx]))
        self.fields = [
            self.ax,
            self.ay,
            self.az,
            self.aw,
            self.bx,
            self.by,
            self.bz,
            self.bw,
            self.cx,
            self.cy,
            self.cz,
            self.cw,
            self.dx,
            self.dy,
            self.dz,
            self.dw,
        ]
        return self
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
        self.Create()
    def Write():
        pass

class CUInt32(object):
    """docstring for CUInt32."""
    def __init__(self, CR2WFILE, val = None):
        super(CUInt32, self).__init__()
        self.val = val
        self.type = 'Uint32'
    def Read(self, f, size):
        self.val = readU32(f)
    def Write():
        pass

class CUInt16(object):
    """docstring for CUInt16."""
    def __init__(self, CR2WFILE, val = None):
        super(CUInt16, self).__init__()
        self.val = val
        self.type = 'Uint16'
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

        buf_type = self.buffer_type.__name__
        buf_type = "CName" if self.buffer_type == CNAME_INDEX else buf_type
        self.theType = f"CPaddedBuffer:{buf_type}"

    def Read(self, f, size):
        elementcount = ReadBit6(f)
        for _ in range(0, elementcount):
            element = self.buffer_type(self.CR2WFILE)
            element.Read(f, 0)
            self.elements.append(element)
        self.padding = readFloat(f)
    def AddElements(self, elements, padding):
        elementcount = len(elements)
        for _ in range(0, elementcount):
            self.elements.append(elements[_])
        self.padding = padding

class CBufferVLQInt32():
    def __init__(self, CR2WFILE, buffer_type, inner_type = False, theName = None):
        self.buffer_type = buffer_type
        self.inner_type = inner_type
        self.CR2WFILE = CR2WFILE
        self.elements = []
        self.theName = theName
        buf_type = self.buffer_type.__name__
        buf_type = "CName" if self.buffer_type == CNAME_INDEX else buf_type

        if self.inner_type:
            in_type = self.inner_type.__name__
            self.theType = f"CBufferVLQInt32:{buf_type}:{in_type}"
        else:
            self.theType = f"CBufferVLQInt32:{buf_type}"

    def Read(self, f, size):
        elementcount = ReadVLQInt32(f)
        for _ in range(0, elementcount):
            if self.inner_type:
                element = self.buffer_type(self.CR2WFILE, self.inner_type)
                element.theType = self.theType[16:]
            else:
                element = self.buffer_type(self.CR2WFILE)
                element.theType = self.theType[16:]
            element.Read(f, 0)
            self.elements.append(element)