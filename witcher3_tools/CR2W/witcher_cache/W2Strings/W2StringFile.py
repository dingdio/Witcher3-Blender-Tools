from __future__ import annotations

import logging
import os
import struct

from .W2Language import (
    W2_MAGIC_BY_FILE_KEY,
    language_from_key,
    language_handle_from_filename,
)

log = logging.getLogger(__name__)


class W2StringsParseError(Exception):
    """Raised when a .w2strings file cannot be parsed."""


def _ensure_available(buf, pos: int, size: int, what: str) -> None:
    if pos < 0 or size < 0 or pos + size > len(buf):
        raise W2StringsParseError(f"unexpected end of file while reading {what}")


def _rotate_left_u16(value: int, bits: int) -> int:
    value &= 0xFFFF
    bits %= 16
    return ((value << bits) | (value >> (16 - bits))) & 0xFFFF


def _read_encoded_s32(buf, pos: int):
    _ensure_available(buf, pos, 1, "encoded integer")
    op = buf[pos]
    pos += 1
    value = op & 0x3F
    if op & 0x40:
        shift = 6
        while True:
            if shift > 27:
                raise W2StringsParseError("encoded int overflow")
            _ensure_available(buf, pos, 1, "encoded integer")
            extra = buf[pos]
            pos += 1
            value |= (extra & 0x7F) << shift
            shift += 7
            if not (extra & 0x80):
                break
    if op & 0x80:
        value = -value
    return value, pos


def _read_encoded_string(buf, pos: int):
    length, pos = _read_encoded_s32(buf, pos)
    if length < 0:
        byte_count = -length
        if byte_count >= 0x10000:
            raise W2StringsParseError("encoded string too long")
        _ensure_available(buf, pos, byte_count, "encoded string")
        raw = bytes(buf[pos:pos + byte_count])
        pos += byte_count
        try:
            text = raw.decode("cp1252", errors="replace")
        except Exception:
            text = raw.decode("latin-1", errors="replace")
    else:
        if length >= 0x10000:
            raise W2StringsParseError("encoded string too long")
        byte_count = length * 2
        _ensure_available(buf, pos, byte_count, "encoded string")
        raw = bytes(buf[pos:pos + byte_count])
        pos += byte_count
        try:
            text = raw.decode("utf-16-le", errors="replace")
        except Exception:
            text = raw.decode("latin-1", errors="replace")
    null_idx = text.find("\x00")
    if null_idx >= 0:
        text = text[:null_idx]
    return text, pos


def _read_encoded_string_buffer(buf, pos: int):
    length, pos = _read_encoded_s32(buf, pos)
    if length < 0:
        raise W2StringsParseError("negative string-buffer length not implemented")
    if length >= 0x10000:
        raise W2StringsParseError("string buffer too long")
    byte_count = length * 2
    _ensure_available(buf, pos, byte_count, "encoded string buffer")
    raw = bytes(buf[pos:pos + byte_count])
    pos += byte_count
    return raw, length, pos


class W2StringFile:
    """Decoded .w2strings payload."""

    def __init__(self):
        self.version = 0
        self.encryption_key = 0
        self.magic = 0
        self.source_path = ""
        self.filename_language = ""
        self.language = None
        self.keys = {}
        self.texts = {}

    def __len__(self):
        return len(self.texts)

    @classmethod
    def from_file(cls, path):
        instance = cls()
        instance.source_path = str(path or "")
        instance.filename_language = language_handle_from_filename(path)
        with open(path, "rb") as handle:
            data = handle.read()
        instance._parse(data)
        return instance

    def Read(self, stream):
        self.source_path = str(getattr(stream, "name", "") or "")
        self.filename_language = language_handle_from_filename(self.source_path)
        data = stream.readAll() if hasattr(stream, "readAll") else stream.read()
        self._parse(data)

    def _parse(self, data: bytes):
        view = memoryview(data)
        if len(view) < 8:
            raise W2StringsParseError("file too short")

        pos = 0
        self.version, = struct.unpack_from("<I", view, pos)
        pos += 4

        encryption_key = 0
        if self.version >= 114:
            _ensure_available(view, pos, 2, "encryption key high word")
            high, = struct.unpack_from("<H", view, pos)
            pos += 2
            encryption_key |= (high & 0xFFFF) << 16

        key_count, pos = _read_encoded_s32(view, pos)
        if key_count < 0:
            raise W2StringsParseError("negative key count")

        raw_keys = []
        for _idx in range(key_count):
            name, pos = _read_encoded_string(view, pos)
            _ensure_available(view, pos, 4, "string key index")
            index, = struct.unpack_from("<I", view, pos)
            pos += 4
            raw_keys.append((name, index))

        if self.version >= 114:
            _ensure_available(view, pos, 2, "encryption key low word")
            low, = struct.unpack_from("<H", view, pos)
            pos += 2
            encryption_key |= low & 0xFFFF

        self.encryption_key = encryption_key
        self.magic = W2_MAGIC_BY_FILE_KEY.get(encryption_key, 0)
        self.language = language_from_key(encryption_key, self.filename_language)

        for name, index in raw_keys:
            self.keys[name] = (index ^ self.magic) & 0xFFFFFFFF

        file_strings_hash_expected = None
        if self.version >= 200:
            _ensure_available(view, pos, 4, "strings hash")
            stored_hash, = struct.unpack_from("<I", view, pos)
            pos += 4
            file_strings_hash_expected = (stored_hash ^ self.magic) & 0xFFFFFFFF

        string_count, pos = _read_encoded_s32(view, pos)
        if string_count < 0:
            raise W2StringsParseError("negative string count")

        actual_hash = 0
        for _idx in range(string_count):
            _ensure_available(view, pos, 4, "string id")
            xored_index, = struct.unpack_from("<I", view, pos)
            pos += 4
            index = (xored_index ^ self.magic) & 0xFFFFFFFF

            buffer, char_count, pos = _read_encoded_string_buffer(view, pos)
            buffer = bytearray(buffer)
            string_key = (self.magic >> 8) & 0xFFFF

            for j in range(0, len(buffer), 2):
                pair = buffer[j] | (buffer[j + 1] << 8)
                actual_hash = (actual_hash + pair) & 0xFFFFFFFF

                if self.version >= 200:
                    char_key = ((char_count + 1) * string_key) & 0xFFFF
                    buffer[j] ^= char_key & 0xFF
                    buffer[j + 1] ^= (char_key >> 8) & 0xFF
                    string_key = _rotate_left_u16(string_key, 1)
                else:
                    buffer[j] ^= string_key & 0xFF
                    buffer[j + 1] ^= (string_key >> 8) & 0xFF
                    string_key = (string_key + 1) & 0xFFFF

            if index == 65434 and actual_hash and char_count >= 52:
                text = bytes(buffer[:104]).decode("utf-16-le", errors="replace")
            else:
                text = bytes(buffer).decode("utf-16-le", errors="replace")
            null_idx = text.find("\x00")
            if null_idx >= 0:
                text = text[:null_idx]
            self.texts[index] = text

        if 114 <= self.version < 200 and pos + 4 <= len(view):
            file_strings_hash_expected, = struct.unpack_from("<I", view, pos)
            pos += 4

        if file_strings_hash_expected is not None and file_strings_hash_expected != actual_hash:
            log.warning(
                "W2 strings hash mismatch (expected 0x%08X, got 0x%08X); strings still decoded",
                file_strings_hash_expected,
                actual_hash,
            )


W2StringsFile = W2StringFile


def find_w2_strings_files(roots):
    """Walk W2 roots looking for CookedPC/*.w2strings files."""

    if roots is None:
        roots = []
    elif isinstance(roots, (str, os.PathLike)):
        roots = [roots]

    found = []
    seen = set()

    def _record(path):
        key = os.path.normcase(os.path.normpath(str(path)))
        if key in seen:
            return
        seen.add(key)
        found.append(str(path))

    for root in roots:
        try:
            root_str = str(root)
        except Exception:
            continue
        if not root_str:
            continue
        if os.path.isfile(root_str):
            if root_str.lower().endswith(".w2strings"):
                _record(root_str)
            continue
        if not os.path.isdir(root_str):
            continue

        cooked = os.path.join(root_str, "CookedPC")
        if os.path.isdir(cooked):
            try:
                entries = sorted(os.listdir(cooked))
            except Exception:
                entries = []
            for name in entries:
                if name.lower().endswith(".w2strings"):
                    _record(os.path.join(cooked, name))

        try:
            for name in sorted(os.listdir(root_str)):
                if name.lower().endswith(".w2strings"):
                    _record(os.path.join(root_str, name))
        except Exception:
            pass
    return found
