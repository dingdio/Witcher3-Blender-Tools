from __future__ import annotations

import logging
import os
import struct

from ....extension_paths import get_audio_root
from ..W2Strings.W2Language import (
    W2_MAGIC_BY_FILE_KEY,
    language_from_key,
    language_handle_from_filename,
)

log = logging.getLogger(__name__)


class W2SpeechParseError(Exception):
    """Raised when a .w2speech file cannot be parsed."""


def _read_exact(handle, size: int, what: str) -> bytes:
    data = handle.read(size)
    if len(data) != size:
        raise W2SpeechParseError(f"unexpected end of file while reading {what}")
    return data


def _read_u8(handle) -> int:
    return _read_exact(handle, 1, "byte")[0]


def _read_bit6(handle, return_len: bool = False):
    result = 0
    shift = 0
    i = 1

    while True:
        b = _read_u8(handle)
        if b == 128:
            return (0, i - 1) if return_len else 0

        s = 6
        mask = 255
        if b > 127:
            mask = 127
            s = 7
        elif b > 63 and i == 1:
            mask = 63

        result |= (b & mask) << shift
        shift += s
        i += 1

        if b < 64 or (i >= 3 and b < 128):
            break

    return (result, i - 1) if return_len else result


def _valid_map_offset(value: int, file_size: int) -> bool:
    return 0 < int(value or 0) < max(file_size - 2, 0)


def _read_legacy_map_offset(handle, file_size: int) -> int:
    if file_size < 8:
        raise W2SpeechParseError("file too short for legacy footer")

    candidates = []
    handle.seek(file_size - 8)
    raw8 = _read_exact(handle, 8, "legacy footer")
    candidates.append(struct.unpack("<Q", raw8)[0])
    candidates.append(struct.unpack("<I", raw8[4:8])[0])
    candidates.append(struct.unpack("<I", raw8[:4])[0])

    handle.seek(file_size - 4)
    candidates.append(struct.unpack("<I", _read_exact(handle, 4, "legacy map offset"))[0])

    seen = set()
    for value in candidates:
        if value in seen:
            continue
        seen.add(value)
        if _valid_map_offset(value, file_size):
            return value

    raise W2SpeechParseError("could not locate legacy speech offset map")


def _copy_range(source_path: str, offset: int, size: int, output) -> None:
    if not size:
        return
    remaining = int(size)
    with open(source_path, "rb") as handle:
        handle.seek(int(offset))
        while remaining > 0:
            chunk = handle.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            output.write(chunk)
            remaining -= len(chunk)


def w2_voice_base_name(value) -> str:
    text = "" if value is None else str(value).strip()
    stem = os.path.splitext(os.path.basename(text))[0] if text else ""
    if stem.lower().startswith("vo_id"):
        return stem
    try:
        return f"VO_ID{int(stem or text)}"
    except Exception:
        return stem or text


class W2SpeechEntry:
    def __init__(
        self,
        bundle=None,
        id=None,
        encrypted_id=None,
        vo_offs=None,
        vo_size=None,
        mp2_offs=None,
        mp2_size=None,
        lipsync_offs=None,
        lipsync_size=None,
        duration=None,
        compression=None,
    ):
        self.bundle = bundle
        self.id = id
        self.Hash = id
        self.encrypted_id = encrypted_id
        self.vo_offs = vo_offs or 0
        self.vo_size = vo_size or 0
        self.mp2_offs = mp2_offs or 0
        self.mp2_size = mp2_size or 0
        self.lipsync_offs = lipsync_offs or 0
        self.lipsync_size = lipsync_size or 0
        self.duration = duration
        self.compression = compression

        self.name = id
        if self.mp2_size:
            self.page_offset = self.mp2_offs
            self.z_size = self.mp2_size
        elif self.lipsync_size:
            self.page_offset = self.lipsync_offs
            self.z_size = self.lipsync_size
        else:
            self.page_offset = self.vo_offs
            self.z_size = self.vo_size

        self.size = (self.vo_size or 0) + (self.lipsync_size or 0)
        self.compression_type = "None"

    def extract(self, output):
        if not self.bundle:
            return
        _copy_range(self.bundle.ArchiveAbsolutePath, self.page_offset, self.z_size, output)

    def extract_pair(self, output_dir=None, file_name=None, lipsync_output_dir=None):
        if not self.bundle:
            return "", ""

        base_path = output_dir or os.path.join(get_audio_root(create=True), "W2Speech")
        lipsync_base_path = lipsync_output_dir or base_path
        os.makedirs(base_path, exist_ok=True)
        os.makedirs(lipsync_base_path, exist_ok=True)

        base_name = w2_voice_base_name(file_name or self.id)
        mp2_path = ""
        dat_path = ""

        if self.mp2_size:
            mp2_path = os.path.join(base_path, base_name + ".mp2")
            with open(mp2_path, "wb") as output:
                _copy_range(self.bundle.ArchiveAbsolutePath, self.mp2_offs, self.mp2_size, output)

        if self.lipsync_size:
            dat_path = os.path.join(lipsync_base_path, base_name + ".dat")
            with open(dat_path, "wb") as output:
                _copy_range(self.bundle.ArchiveAbsolutePath, self.lipsync_offs, self.lipsync_size, output)

        return dat_path, mp2_path

    def extract_to_file(self, file_name=None, output_dir=None):
        dat_path, mp2_path = self.extract_pair(output_dir=output_dir, file_name=file_name)
        return mp2_path or dat_path


class W2Speech:
    LEGACY_ENTRY_SIZE = 20

    def __init__(self, filePath=None, id="", version=0, language_key=None, item_infos=None):
        super().__init__()
        self.filePath = filePath
        self.ArchiveAbsolutePath = filePath
        self.id = id
        self.version = version
        self.language_key = language_key
        self.magic = 0
        self.language = None
        self.filename_language = language_handle_from_filename(filePath)
        self.map_offset = 0
        self.item_count = 0
        self.item_infos = list(item_infos or [])

        if filePath:
            self.read(filePath)

    def read(self, filepath):
        file_size = os.path.getsize(filepath)
        with open(filepath, "rb") as handle:
            first4 = _read_exact(handle, 4, "header")
            if first4 in (b"CPSW", b"WSPC"):
                raise W2SpeechParseError("WSPC/CPSW speech format is not supported by the W2 legacy reader")

            self.version = struct.unpack("<I", first4)[0]
            key1 = struct.unpack("<H", _read_exact(handle, 2, "language key high word"))[0]
            self.map_offset = _read_legacy_map_offset(handle, file_size)

            if self.map_offset < 2:
                raise W2SpeechParseError("invalid legacy speech map offset")
            handle.seek(self.map_offset - 2)
            key2 = struct.unpack("<H", _read_exact(handle, 2, "language key low word"))[0]

            file_key = ((key1 & 0xFFFF) << 16) | (key2 & 0xFFFF)
            magic = W2_MAGIC_BY_FILE_KEY.get(file_key, 0)
            language = language_from_key(file_key, self.filename_language)

            handle.seek(self.map_offset)
            item_count, item_count_len = _read_bit6(handle, return_len=True)
            max_entries = max((file_size - handle.tell()) // self.LEGACY_ENTRY_SIZE, 0)
            if item_count < 0 or item_count > max_entries:
                raise W2SpeechParseError(
                    f"invalid legacy speech item count {item_count} at 0x{self.map_offset:08X}"
                )

            item_infos = []
            for _index in range(item_count):
                raw = _read_exact(handle, self.LEGACY_ENTRY_SIZE, "legacy speech entry")
                encrypted_id, vo_offs, vo_size, lipsync_offs, lipsync_size = struct.unpack("<IIIII", raw)
                speech_id = (encrypted_id ^ magic) & 0xFFFFFFFF

                duration = None
                compression = None
                mp2_offs = vo_offs
                mp2_size = vo_size

                if vo_size >= 12 and 0 < vo_offs < file_size:
                    current_pos = handle.tell()
                    try:
                        handle.seek(vo_offs)
                        data_size = struct.unpack("<I", _read_exact(handle, 4, "voice buffer size"))[0]
                        if 0 < data_size <= max(vo_size - 12, 0) and vo_offs + 4 + data_size <= file_size:
                            mp2_offs = vo_offs + 4
                            mp2_size = data_size
                            handle.seek(mp2_offs + mp2_size)
                            if handle.tell() + 8 <= file_size:
                                duration = struct.unpack("<f", _read_exact(handle, 4, "voice duration"))[0]
                                compression = struct.unpack("<I", _read_exact(handle, 4, "voice compression"))[0]
                    except Exception:
                        mp2_offs = vo_offs
                        mp2_size = vo_size
                    finally:
                        handle.seek(current_pos)

                if vo_size or lipsync_size:
                    item_infos.append(
                        W2SpeechEntry(
                            self,
                            speech_id,
                            encrypted_id,
                            vo_offs,
                            vo_size,
                            mp2_offs,
                            mp2_size,
                            lipsync_offs,
                            lipsync_size,
                            duration,
                            compression,
                        )
                    )

        self.filePath = filepath
        self.ArchiveAbsolutePath = filepath
        self.language_key = file_key
        self.magic = magic
        self.language = language
        self.item_count = item_count
        self.item_infos = item_infos

        log.debug(
            "Loaded W2 speech %s: version=%s language_key=0x%08X items=%d voiced=%d map=0x%08X",
            filepath,
            self.version,
            self.language_key,
            self.item_count,
            len(self.item_infos),
            self.map_offset,
        )
