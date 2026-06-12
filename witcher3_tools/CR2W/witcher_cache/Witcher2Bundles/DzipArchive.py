from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import datetime as _dt
import logging
import os
import shutil
import struct
import tempfile

log = logging.getLogger(__name__)


class InvalidDzipException(Exception):
    pass


def _read_exact(handle, size: int) -> bytes:
    data = handle.read(size)
    if len(data) != size:
        raise EOFError(f"Unexpected end of DZIP while reading {size} bytes")
    return data


def _read_u16(handle) -> int:
    return struct.unpack("<H", _read_exact(handle, 2))[0]


def _read_u32(handle) -> int:
    return struct.unpack("<I", _read_exact(handle, 4))[0]


def _read_i64(handle) -> int:
    return struct.unpack("<q", _read_exact(handle, 8))[0]


def _read_u64(handle) -> int:
    return struct.unpack("<Q", _read_exact(handle, 8))[0]


def _windows_filetime_to_timestamp(filetime: int) -> float:
    try:
        return max(0.0, (int(filetime) - 116444736000000000) / 10000000.0)
    except Exception:
        return 0.0


def _decode_ascii_z(raw: bytes) -> str:
    if raw.endswith(b"\0"):
        raw = raw[:-1]
    return raw.decode("ascii", errors="replace").replace("/", "\\")


def _entries_hash(entries) -> int:
    value = 0x00000000FFFFFFFF
    prime = 0x00000100000001B3
    mask = 0xFFFFFFFFFFFFFFFF
    for entry in entries:
        if entry.name:
            for byte in entry.name.encode("ascii", errors="replace"):
                value ^= byte
                value = (value * prime) & mask
            value ^= len(entry.name)
            value = (value * prime) & mask

        value ^= entry.timestamp_filetime & mask
        value = (value * prime) & mask
        value ^= entry.size & mask
        value = (value * prime) & mask
        value ^= entry.offset & mask
        value = (value * prime) & mask
        value ^= entry.zsize & mask
        value = (value * prime) & mask
    return value


def _lzf_decompress(data: bytes, expected_size: int) -> bytes:
    output = bytearray(expected_size)
    i = 0
    o = 0
    input_len = len(data)

    while i < input_len:
        control = data[i]
        i += 1

        if control < (1 << 5):
            length = control + 1
            if o + length > expected_size or i + length > input_len:
                raise InvalidDzipException("Invalid LZF literal run")
            output[o:o + length] = data[i:i + length]
            i += length
            o += length
            continue

        length = control >> 5
        offset = (control & 0x1F) << 8
        if length == 7:
            if i >= input_len:
                raise InvalidDzipException("Invalid LZF length extension")
            length += data[i]
            i += 1
        length += 2

        if i >= input_len:
            raise InvalidDzipException("Invalid LZF offset")
        offset |= data[i]
        i += 1

        offset = o - 1 - offset
        if offset < 0 or o + length > expected_size:
            raise InvalidDzipException("Invalid LZF back-reference")

        while length > 0:
            output[o] = output[offset]
            o += 1
            offset += 1
            length -= 1

    if o != expected_size:
        raise InvalidDzipException(f"LZF size mismatch: got {o}, expected {expected_size}")
    return bytes(output)


@dataclass
class DzipItem:
    bundle: "DzipArchive | None" = None
    name: str = ""
    timestamp_filetime: int = 0
    size: int = 0
    offset: int = 0
    zsize: int = 0

    @property
    def Name(self) -> str:
        return self.name

    @property
    def Size(self) -> int:
        return self.size

    @property
    def compression_type(self) -> str:
        return "LZF"

    @property
    def date_string(self) -> str:
        ts = _windows_filetime_to_timestamp(self.timestamp_filetime)
        if not ts:
            return ""
        try:
            return _dt.datetime.fromtimestamp(ts).strftime("%d/%m/%Y %H:%M:%S")
        except Exception:
            return ""

    def _read_block_offsets(self, handle):
        if self.size <= 0:
            return []
        block_count = int((self.size + 0xFFFF) >> 16)
        handle.seek(self.offset)
        offsets = [self.offset + _read_u32(handle) for _ in range(block_count)]
        offsets.append(self.offset + self.zsize)
        return offsets

    def extract(self, output):
        if self.bundle is None:
            raise InvalidDzipException("DZIP item has no owning archive")
        with open(self.bundle.ArchiveAbsolutePath, "rb") as handle:
            if self.size <= 0:
                return
            offsets = self._read_block_offsets(handle)
            remaining = self.size
            for index in range(len(offsets) - 1):
                compressed_size = offsets[index + 1] - offsets[index]
                if compressed_size < 0:
                    raise InvalidDzipException(f"Invalid DZIP block offsets for {self.name}")
                handle.seek(offsets[index])
                compressed = _read_exact(handle, compressed_size)
                expected_size = min(0x10000, remaining)
                output.write(_lzf_decompress(compressed, expected_size))
                remaining -= expected_size

    def extract_to_file(self, file_name: str):
        if not file_name:
            raise ValueError("file_name cannot be empty")

        from ...common_blender import win_safe_path

        safe_name = win_safe_path(file_name)
        dir_name = os.path.dirname(safe_name)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)

        temp_fd, temp_path = tempfile.mkstemp(dir=dir_name or None)
        try:
            with os.fdopen(temp_fd, "wb") as temp_file:
                self.extract(temp_file)
            shutil.move(temp_path, safe_name)
        except Exception:
            try:
                os.remove(temp_path)
            except OSError:
                pass
            raise

        ts = _windows_filetime_to_timestamp(self.timestamp_filetime)
        if ts:
            try:
                os.utime(safe_name, (ts, ts))
            except OSError:
                pass
        return file_name


class DzipArchive:
    IDString = b"DZIP"
    HeaderSize = 32

    def __init__(self, filename: str | None = None):
        self.ArchiveAbsolutePath = filename
        self.Items = OrderedDict()
        self.Version = 0
        self.entry_table_hash = 0
        if filename:
            self.Read()

    @property
    def TypeName(self):
        return "DZIP"

    def Read(self):
        self.Items = OrderedDict()
        with open(self.ArchiveAbsolutePath, "rb") as handle:
            magic = _read_exact(handle, 4)
            if magic != self.IDString:
                raise InvalidDzipException("DZIP header mismatch.")

            self.Version = _read_u32(handle)
            if self.Version < 2:
                raise InvalidDzipException(f"Unsupported DZIP version: {self.Version}")

            entry_count = _read_u32(handle)
            _unknown = _read_u32(handle)
            entry_table_offset = _read_i64(handle)
            self.entry_table_hash = _read_u64(handle)

            handle.seek(entry_table_offset)
            entries = []
            for _index in range(entry_count):
                name_len = _read_u16(handle)
                name = _decode_ascii_z(_read_exact(handle, name_len))
                item = DzipItem(
                    bundle=self,
                    name=name,
                    timestamp_filetime=_read_i64(handle),
                    size=_read_i64(handle),
                    offset=_read_i64(handle),
                    zsize=_read_i64(handle),
                )
                entries.append(item)
                if item.name not in self.Items:
                    self.Items[item.name] = item
                else:
                    log.warning(
                        "DZIP '%s' has duplicate resource '%s'; only the first entry is indexed.",
                        self.ArchiveAbsolutePath,
                        item.name,
                    )

            actual_hash = _entries_hash(entries)
            if actual_hash != self.entry_table_hash:
                raise InvalidDzipException("Bad DZIP entry table hash.")
