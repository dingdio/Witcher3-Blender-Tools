import struct


def read_u8_at(data: bytes, offset: int, context: str = "W2 data"):
    if offset + 1 > len(data):
        raise ValueError(f"Unexpected end of {context}")
    return data[offset], offset + 1


def read_u16_at(data: bytes, offset: int, context: str = "W2 data"):
    if offset + 2 > len(data):
        raise ValueError(f"Unexpected end of {context}")
    return struct.unpack_from("<H", data, offset)[0], offset + 2


def read_i32_at(data: bytes, offset: int, context: str = "W2 data"):
    if offset + 4 > len(data):
        raise ValueError(f"Unexpected end of {context}")
    return struct.unpack_from("<i", data, offset)[0], offset + 4


def read_u32_at(data: bytes, offset: int, context: str = "W2 data"):
    if offset + 4 > len(data):
        raise ValueError(f"Unexpected end of {context}")
    return struct.unpack_from("<I", data, offset)[0], offset + 4


def read_string_at(data: bytes, offset: int, length: int, context: str = "W2 data"):
    if length < 0 or offset + length > len(data):
        raise ValueError(f"Invalid {context} string length")
    return data[offset:offset + length].decode("ascii", errors="replace"), offset + length


def read_encoded_string_at(data: bytes, offset: int, context: str = "W2 data"):
    size, offset = read_u8_at(data, offset, context)
    return read_string_at(data, offset, size - 128, context)
