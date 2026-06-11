import struct
import math
import weakref


def _decode_cr2w_text(raw_bytes):
    """Decode CR2W string bytes with a tolerant fallback for legacy encodings."""
    if not raw_bytes:
        return ""
    if isinstance(raw_bytes, bytearray):
        raw_bytes = bytes(raw_bytes)
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            return raw_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw_bytes.decode("utf-8", errors="replace")

class TypeFormat:
    SByte = '<b'
    Byte = '<B'
    Int16 = '<h'
    UInt16 = '<H'
    Int32 = '<i'
    UInt32 = '<I'
    Int64 = '<l'
    UInt64 = '<L'
    Single = '<f'
    Double = '<d'

def findEndOfMot(f):
    while (f.tell()+4 < FileSize(f)):
        filesize = FileSize(f)
        tell = f.tell();
        if (f.tell() == 544501613):
            f.seek(-4, 1); break;
        f.seek(1, 1);

# Parsers call FileSize per property read (hundreds of thousands of times per
# entity import) and every caller reads a file that is not growing, so cache
# the size per stream object. WeakKey so closed/discarded streams drop out.
_file_size_cache = weakref.WeakKeyDictionary()


def FileSize(f):
    size = _file_size_cache.get(f)
    if size is not None:
        return size
    old_file_position = f.tell()
    f.seek(0, 2)
    size = f.tell()
    f.seek(old_file_position, 0)
    try:
        _file_size_cache[f] = size
    except TypeError:
        pass
    return size

def readString(inFile):
    chars = []
    while True:
        c = inFile.read(1)
        if c == chr(0):
            return "".join(chars)
        chars.append(c)

def getString(file):
    # Read NUL-terminated string in blocks instead of byte-at-a-time
    raw = bytearray()
    while True:
        block = file.read(64)
        if not block:
            break
        idx = block.find(0)
        if idx >= 0:
            raw.extend(block[:idx])
            overshoot = len(block) - (idx + 1)
            if overshoot:
                file.seek(-overshoot, 1)
            break
        raw.extend(block)
    return _decode_cr2w_text(raw)

def getStringOfLen(file, len):
    if len <= 0:
        return ""
    raw = file.read(len)
    return _decode_cr2w_text(raw)


def read_u8_at(data: bytes, offset: int, context: str = "binary data"):
    if offset + 1 > len(data):
        raise ValueError(f"Unexpected end of {context}")
    return data[offset], offset + 1


def read_u16_at(data: bytes, offset: int, context: str = "binary data"):
    if offset + 2 > len(data):
        raise ValueError(f"Unexpected end of {context}")
    return struct.unpack_from("<H", data, offset)[0], offset + 2


def read_i32_at(data: bytes, offset: int, context: str = "binary data"):
    if offset + 4 > len(data):
        raise ValueError(f"Unexpected end of {context}")
    return struct.unpack_from("<i", data, offset)[0], offset + 4


def read_u32_at(data: bytes, offset: int, context: str = "binary data"):
    if offset + 4 > len(data):
        raise ValueError(f"Unexpected end of {context}")
    return struct.unpack_from("<I", data, offset)[0], offset + 4


def read_ascii_at(data: bytes, offset: int, length: int, context: str = "binary data"):
    if length < 0 or offset + length > len(data):
        raise ValueError(f"Invalid {context} string length")
    return data[offset:offset + length].decode("ascii", errors="replace"), offset + length


def read_witcher2_encoded_string_at(data: bytes, offset: int, context: str = "Witcher 2 data"):
    size, offset = read_u8_at(data, offset, context)
    return read_ascii_at(data, offset, size - 128, context)

def read_wstring(inFile, chunk_len = 0x100, address = 0):
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
    inFile.seek(address+stringSize+offset)
    return wstring

def readU32(inFile):
    return struct.unpack('I', inFile.read(4))[0]

def readU32Check(f, pos):
    orignal_pos = f.tell()
    f.seek(pos,0)
    the_uint = struct.unpack('I', f.read(4))[0]
    f.seek(orignal_pos, 0)
    return the_uint

def readInt16(file):
    return struct.unpack(TypeFormat.Int16, file.read(2))[0]

def readInt32(file):
    return struct.unpack(TypeFormat.Int32, file.read(4))[0]

def readUShort(file):
    return struct.unpack(TypeFormat.UInt16, file.read(2))[0]

def ReadVLQInt32(file):
    #bytes = file.read(3)
    b1 = readUByte(file);
    sign = (b1 & 128) == 128;
    next = (b1 & 64) == 64;
    size = b1 % 128 % 64;
    offset = 6;
    while (next):
        b = readUByte(file)#file.read(1);
        size = (b % 128) << offset | size;
        next = (b & 128) == 128;
        offset += 7;
    if sign:
        return size * -1
    else:
        return size
    #return sign ? size * -1 : size;

def ReadBit6(file, returnLen = False):
    result = 0;
    shift = 0;
    b = 0; # byte
    i = 1;

    while True:
        b = readUByte(file) #stream.ReadByte();
        if (b == 128):
            return (0, i-1) if returnLen else 0;#return 0;
        s = 6; # byte
        mask = 255; # byte
        if (b > 127):
            mask = 127;
            s = 7;
        elif (b > 63):
            if (i == 1):
                mask = 63;
        result = result | ((b & mask) << shift);
        shift = shift + s;
        i = i + 1;

        #(!(b < 64 || (i >= 3 && b < 128)))
        if (b < 64 or (i >= 3 and b < 128) ):
            break;
        
    return (result, i-1) if returnLen else result;

def ReadFloat24(file):
    bytes = file.read(3)
    bytes = b"\x00"+bytes
    thefloat = struct.unpack('f', bytes)[0]
    return thefloat
    # pad = 0;
    # b1 = file.read(1)
    # b2 = file.read(1)
    # b3 = file.read(1)
    # return (b3 << 24) |(b2 << 16) | (b1 << 8) |(pad);

def ReadUlong40(file):
    bytes = file.read(5)
    int_values = [x for x in bytearray(bytes)]
    bits = int_values[0] << 32 | int_values[1] << 24 | int_values[2] << 16 | int_values[3] << 8 | int_values[4]
    return bits

def ReadUlong48(file):
    bytes = file.read(6)
    int_values = [x for x in bytearray(bytes)]
    #print(int_values)
    bits = int_values[0] << 40 | int_values[1] << 32 | int_values[2] << 24 | int_values[3] << 16 | int_values[4] << 8 | int_values[5]
    #240208922541479
    return bits

def ReadFloat16(file):
    bytes = file.read(2)
    bytes = b"\x00\x00"+bytes
    thefloat = struct.unpack('f', bytes)[0]
    return thefloat
    # pad = 0;
    # b1 = file.read(1)
    # b2 = file.read(1)
    # return (int(b2, 10) << 32) | (int(b1, 10) << 24) |(pad) |(pad);

def readUShortCheck(f, pos):
    orignal_pos = f.tell()
    f.seek(pos,0)
    the_uint = struct.unpack(TypeFormat.UInt16, f.read(2))[0]
    f.seek(orignal_pos, 0)
    return the_uint

def readSByte(file):
    return struct.unpack(TypeFormat.SByte, file.read(1))[0]

def readUByte(file):
    return struct.unpack(TypeFormat.Byte, file.read(1))[0]

def readUChar(file):
    return struct.unpack(TypeFormat.Byte, file.read(1))[0]

def readUByteCheck(f, pos):
    orignal_pos = f.tell()
    f.seek(pos,0)
    the_uint = struct.unpack(TypeFormat.Byte, f.read(1))[0]
    f.seek(orignal_pos, 0)
    return the_uint

def readI32(inFile):
    return struct.unpack('i', inFile.read(4))[0]

def readULong(inFile):
    return struct.unpack('L', inFile.read(4))[0]

def readU64(inFile):
    return struct.unpack('Q', inFile.read(8))[0]

def readFloat(inFile):
    return struct.unpack('f', inFile.read(4))[0]

def readFloatCheck(f, pos):
    orignal_pos = f.tell()
    f.seek(pos,0)
    the_uint = struct.unpack('f', f.read(4))[0]
    f.seek(orignal_pos, 0)
    return the_uint

def detectedFloat(f, offset):
    return abs(readFloatCheck(f,offset)) == 0 or (abs(readFloatCheck(f,offset)) > 0.0000000000000001 and abs(readFloatCheck(f,offset)) < 100000000000000000);


# def readSingle(file):
#     numberBin = file.read(4)
#     single = struct.unpack(TypeFormat.Single, numberBin)[0]
#     return single

def skipToNextLine(f):
    while (f.tell() %16 != 0):
        f.seek(1, 1)

def skipPadding(f):
    while (readUByteCheck(f, f.tell()) == 0 and f.tell() + 1 < FileSize(f)):
        f.seek(1,1)

def wRot(frame):
    RotationX = frame.RotationX
    RotationY = frame.RotationY
    RotationZ = frame.RotationZ

    RotationW = 1.0 - (RotationX * RotationX + RotationY * RotationY + RotationZ * RotationZ)
    if (RotationW > 0.0):
        RotationW = math.sqrt(RotationW) #(float)Sqrt(RotationW);
    else:
        RotationW = 0.0
    return RotationW
