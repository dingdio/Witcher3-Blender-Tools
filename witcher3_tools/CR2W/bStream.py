import struct
import io
import logging
log = logging.getLogger(__name__)

# This is free and unencumbered software released into the public domain.
#
# Anyone is free to copy, modify, publish, use, compile, sell, or
# distribute this software, either in source code form or as a compiled
# binary, for any purpose, commercial or non-commercial, and by any
# means.
#
# In jurisdictions that recognize copyright laws, the author or authors
# of this software dedicate any and all copyright interest in the
# software to the public domain. We make this dedication for the benefit
# of the public at large and to the detriment of our heirs and
# successors. We intend this dedication to be an overt act of
# relinquishment in perpetuity of all present and future rights to this
# software under copyright law.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
# IN NO EVENT SHALL THE AUTHORS BE LIABLE FOR ANY CLAIM, DAMAGES OR
# OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE,
# ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
# OTHER DEALINGS IN THE SOFTWARE.
#
# For more information, please refer to <http://unlicense.org>

class bStream():
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def __init__(self, data=None, path=None, reader = None):
        self.isBuffered = False
        if(path is not None):
            if reader:
                self.fhandle = reader
                self.name = self.fhandle.name
            else:
                try:
                    self.fhandle = open(path, 'r+b')
                except OSError:
                    self.fhandle = open(path, 'wb')
                self.name = self.fhandle.name
        else:
            self.isBuffered = True
            self.fhandle = io.BytesIO(data)
            self.name = "<embedded>"
        self.decoder = 'shift-jis'
        self.endian = '<'

    def getBuffer(self):
        return self.fhandle if not self.isBuffered else None


    # result = ""
    # tmpChar = file.read(1)
    # while ord(tmpChar) != 0:
    #     result += tmpChar.decode('utf-8')
    #     tmpChar =file.read(1)
    # return result
    def readStringZero(self, len = 0):
        inFile = self.fhandle
        str = ""
        if len != 0:
            str = inFile.read(len)
        else:
            chars = []
            while True:
                c = inFile.read(1)
                if c == b"\x00":
                    return "".join(chars)
                chars.append(c.decode("utf-8"))
        return str

    def getStringOfLen(self, len):
        file = self.fhandle
        result = ""
        tmpChar = file.read(1)
        for _ in range(0, len):
            result += tmpChar.decode('utf-8')
            tmpChar =file.read(1)
        return result

    def readString(self, len=0, nullTerm=False):
        if (not nullTerm):
            return self.fhandle.read(len).decode(self.decoder)
        else:
            tString = b''
            curr = self.fhandle.read(1)
            while (curr != b'' and struct.unpack(self.endian+"B", curr)[0] != 0):
                try:
                    tString += curr
                except Exception as e:
                    log.error('Error "%s" while reading string at %s', e, hex(self.fhandle.tell()))
                curr = self.fhandle.read(1)
            return tString.decode(self.decoder)

    def read_wstring(self, chunk_len = 0x100, address = 0, check = False):
        inFile = self.fhandle
        if check:
            pos = inFile.tell()
        #.replace("\x00","")
        if address == 0:
            address = inFile.tell()
        wstring = ''
        offset = 0
        stringSize = 0
        while 1:
            null_found = False
            inFile.seek(address+offset)
            read_bytes = inFile.read(chunk_len)
            try:
                read_bytes = str(read_bytes, 'utf-8')
            except UnicodeDecodeError:
                chunk_len -= 1
                continue
            chunk_len = 0x100
            for i in range(0, len(read_bytes)-1, 2):
                if read_bytes[i] == '\x00' and read_bytes[i+1] == '\x00':
                    null_found = True
                    stringSize += i+2
                    break
                wstring += read_bytes[i]

            if null_found:
                break
            offset += len(read_bytes)
            if offset > 9999: # wut
                break
        if check:
            inFile.seek(pos)
        else:
            inFile.seek(address+stringSize+offset)
        return wstring

    def read(self, count):
        return self.fhandle.read(count)

    def readAll(self):
        return self.fhandle.read()

    def readByte(self): #check
        return struct.unpack(self.endian+'b', self.fhandle.read(1))[0]

    def readUByte(self):
        return struct.unpack(self.endian+'B', self.fhandle.read(1))[0]

    def readULong(self):
        return struct.unpack(self.endian+'L', self.fhandle.read(4))[0]

    def readLong(self):
        return struct.unpack(self.endian+'l', self.fhandle.read(4))[0]

    def readUInt64(self):
        return struct.unpack(self.endian+'Q', self.fhandle.read(8))[0]

    def readInt64(self):
        return struct.unpack(self.endian+'q', self.fhandle.read(8))[0]

    def readUInt32(self):
        try:
            return struct.unpack(self.endian+'I', self.fhandle.read(4))[0]
        except BaseException:
            return None

    def readUInt32Check(self, pos):
        f = self.fhandle
        orignal_pos = f.tell()
        f.seek(pos,0);
        the_uint = struct.unpack('I', f.read(4))[0]
        f.seek(orignal_pos, 0)
        return the_uint

    def readInt32(self):
        return struct.unpack(self.endian+'i', self.fhandle.read(4))[0]

    def readUInt16(self):
        return struct.unpack(self.endian+'H', self.fhandle.read(2))[0]

    def readInt16(self):
        return struct.unpack(self.endian+'h', self.fhandle.read(2))[0]

    def readUInt8(self):
        return struct.unpack(self.endian+'B', self.fhandle.read(1))[0]

    def readInt8(self):
        return struct.unpack(self.endian+'b', self.fhandle.read(1))[0]

    def readFloat(self):
        try:
            return struct.unpack(self.endian+'f', self.fhandle.read(4))[0]
        except BaseException:
            return None

    def ReadHalfFloat(self):
        stream = self
        data = stream.readUInt16() # ushort
        # half (binary16) format IEEE 754-2008
        dataSign = data >> 15
        dataExp = (data >> 10) & 0x001F
        dataFrac = data & 0x03FF

        floatExp = 0
        floatFrac = 0

        if dataExp == 0: # subnormal : (-1)^sign * 2^-14 * 0.frac
            if (dataFrac != 0): # subnormals but non-zeros -> normals in float32
                floatExp = -15 + 127
                while ((dataFrac & 0x200) == 0):
                    dataFrac <<= 1
                    floatExp-=1
                floatFrac = (dataFrac & 0x1FF) << 14
            else:
                floatFrac = 0
                floatExp = 0; # ± 0 -> ± 0
        elif dataExp == 31: # infinity or NaNs : frac ? NaN : (-1)^sign * infinity
            floatExp = 255
            floatFrac = 0x200000 if dataFrac != 0 else 0 # signaling Nan or zero
        else: # normal : (-1)^sign * 2^(exp-15) * 1.frac
            floatExp = dataExp - 15 + 127
            floatFrac = dataFrac << 13
        # single precision floating point (binary32) format IEEE 754-2008
        floatNum = dataSign << 31 | floatExp << 23 | floatFrac
        result_uint = struct.pack(self.endian+'I', floatNum)
        result = struct.unpack(self.endian+'f', result_uint)[0]
        #floatFrac 0.0558776855
        return result #BitConverter.ToSingle(BitConverter.GetBytes(floatNum), 0);




    def readU32s(self, count):
        ret = []
        for x in range(0, count):
            ret.append(self.readUInt32())
        return ret

    def readVec3(self):
        vec = []
        vec.append(self.readFloat())
        vec.append(self.readFloat())
        vec.append(self.readFloat())
        return vec

    def readVec4(self):
        vec = []
        vec.append(self.readFloat())
        vec.append(self.readFloat())
        vec.append(self.readFloat())
        vec.append(self.readFloat())
        return vec
    
    def hex32(self, inverse = False):
        b1 = self.readUInt8()
        b2 = self.readUInt8()
        b3 = self.readUInt8()
        b4 = self.readUInt8()
        if inverse:
            h32 = f'{b4:02x}{b3:02x}{b2:02x}{b1:02x}'
        else:
            h32 = f'{b1:02x}{b2:02x}{b3:02x}{b4:02x}'
        return h32

    def write(self, data):
        return self.fhandle.write(data)

    def writeUInt8(self, int):
        self.fhandle.write(struct.pack(self.endian+"B", int))

    def writeInt8(self, int):
        self.fhandle.write(struct.pack(self.endian+"b", int))

    def writeFloat(self, float):
        self.fhandle.write(struct.pack(self.endian+"f", float))

    def writeUInt16(self, int):
        self.fhandle.write(struct.pack(self.endian+"H", int))

    def writeInt16(self, int):
        self.fhandle.write(struct.pack(self.endian+"h", int))

    def writeUInt32(self, int):
        self.fhandle.write(struct.pack(self.endian+"I", int))

    def writeInt32(self, int):
        self.fhandle.write(struct.pack(self.endian+"i", int))

    def pad(self, count):
        for x in range(0, count):
            self.writeUInt8(0)

    def writeUInt32List(self, list):
        for x in range(0, len(list)):
            self.writeUInt32(list[x])

    def writeUInt32s(self, int, count):
        for x in range(0, count):
            self.writeUInt32(int)

    def writeString(self, str):
        self.fhandle.write(str.encode('ASCII'))

    def seekBack(self, whence=0):
        self.fhandle.seek(self.backPos, whence)

    def seek(self, pos, whence=0):
        self.backPos = self.fhandle.tell()
        self.fhandle.seek(pos, whence)

    def padTo32(self, end):
        nextAligned = (end+(32-1)) & ~(32-1)
        delta = nextAligned - end
        self.pad(delta)

    @staticmethod
    def padTo32Delta(end):
        nextAligned = (end+(32-1)) & ~(32-1)
        return (nextAligned - end)


    def tell(self):
        return self.fhandle.tell()

    def close(self):
        self.fhandle.close()

    def read_point(self):
        return struct.unpack('<fff', self.read(12))
