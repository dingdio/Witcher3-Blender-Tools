import importlib.util
import io
import struct
import sys
import tempfile
import types
import unittest
import zlib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = REPO_ROOT / "witcher3_tools"
CR2W_DIR = TOOLS_DIR / "CR2W"
CACHE_DIR = CR2W_DIR / "witcher_cache" / "CollisionCache"
PACKAGE_NAME = "_collision_cache_version_test"


def _package(name, path):
    module = types.ModuleType(name)
    module.__path__ = [str(path)]
    sys.modules[name] = module


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_package(PACKAGE_NAME, TOOLS_DIR)
_package(f"{PACKAGE_NAME}.CR2W", CR2W_DIR)
_load(f"{PACKAGE_NAME}.CR2W.bStream", CR2W_DIR / "bStream.py")
_load(f"{PACKAGE_NAME}.CR2W.bin_helpers", CR2W_DIR / "bin_helpers.py")
_package(f"{PACKAGE_NAME}.CR2W.witcher_cache", CR2W_DIR / "witcher_cache")
_package(f"{PACKAGE_NAME}.CR2W.witcher_cache.CollisionCache", CACHE_DIR)
_load(
    f"{PACKAGE_NAME}.CR2W.witcher_cache.CollisionCache.CollisionCacheItem",
    CACHE_DIR / "CollisionCacheItem.py",
)
collision_module = _load(
    f"{PACKAGE_NAME}.CR2W.witcher_cache.CollisionCache.Collision_Cache",
    CACHE_DIR / "Collision_Cache.py",
)


READ_BUFFER_SIZE = 0x11223344
LOAD_BUFFER_SIZE = 0x55667788


def _build_cache(version, entries):
    header_size = {1: 0x30, 2: 0x40, 9: 0x30, 10: 0x38, 11: 0x38}[version]
    packed_payloads = [zlib.compress(payload) for _, payload in entries]
    data_offsets = []
    data_end = header_size
    for payload in packed_payloads:
        data_offsets.append(data_end)
        data_end += len(payload)

    encoded_names = [name.encode("utf-8") + b"\0" for name, _ in entries]
    name_offsets = []
    names_size = 0
    for name in encoded_names:
        name_offsets.append(names_size)
        names_size += len(name)

    name_table_offset = data_end
    info_offset = name_table_offset + names_size
    checksum = 0x0123456789ABCDEF
    date = b"12345678"

    if version in (1, 9):
        if version == 1:
            header = b"CC3W" + struct.pack(
                "<I8sIIIIQQ",
                version, date, info_offset, len(entries), name_table_offset,
                names_size, (LOAD_BUFFER_SIZE << 32) | READ_BUFFER_SIZE, checksum,
            )
        else:
            header = b"CC3W" + struct.pack(
                "<I8sIIIIIIQ",
                version, date, info_offset, len(entries), name_table_offset,
                names_size, READ_BUFFER_SIZE, LOAD_BUFFER_SIZE, checksum,
            )
        tokens = b"".join(
            struct.pack(
                "<IIQIIII16s16sB7s",
                name_offset,
                100 + index,
                200 + index,
                data_offset,
                len(packed),
                len(payload),
                300 + index,
                bytes([0x40 + index]) * 16,
                bytes([0x50 + index]) * 16,
                5,
                bytes([0x60 + index]) * 7,
            )
            for index, ((_, payload), packed, data_offset, name_offset) in enumerate(
                zip(entries, packed_payloads, data_offsets, name_offsets)
            )
        )
    elif version == 2:
        header = b"CC3W" + struct.pack(
            "<I8sQQQIIQQ",
            version, date, info_offset, len(entries), name_table_offset,
            names_size, 0, (LOAD_BUFFER_SIZE << 32) | READ_BUFFER_SIZE, checksum,
        )
        tokens = b"".join(
            struct.pack(
                "<IIQQIII16s16sB7s",
                name_offset, 100 + index, 200 + index, data_offset,
                len(packed), len(payload), 300 + index,
                bytes([0x40 + index]) * 16, bytes([0x50 + index]) * 16,
                5, bytes([0x60 + index]) * 7,
            )
            for index, ((_, payload), packed, data_offset, name_offset) in enumerate(
                zip(entries, packed_payloads, data_offsets, name_offsets)
            )
        )
    else:
        header = b"CC3W" + struct.pack(
            "<I8sQIQIIIQ",
            version,
            date,
            info_offset,
            len(entries),
            name_table_offset,
            names_size,
            READ_BUFFER_SIZE,
            LOAD_BUFFER_SIZE,
            checksum,
        )
        tokens = b"".join(
            struct.pack(
                "<IQQIIQQ4fB3x",
                name_offset,
                0x100000000 + index,
                data_offset,
                len(packed),
                len(payload),
                0x200000000 + index,
                0x300000000 + index,
                -1.0 - index,
                -2.0 - index,
                1.0 + index,
                2.0 + index,
                5,
            )
            for index, ((_, payload), packed, data_offset, name_offset) in enumerate(
                zip(entries, packed_payloads, data_offsets, name_offsets)
            )
        )

    return header + b"".join(packed_payloads) + b"".join(encoded_names) + tokens


class CollisionCacheVersionTests(unittest.TestCase):
    def _parse(self, version):
        entries = [
            ("physics\\first.reddest", b"first collision payload" * 4),
            ("physics\\second.reddest", b"second collision payload" * 3),
        ]
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        path = Path(temp_dir.name) / "collision.cache"
        path.write_bytes(_build_cache(version, entries))
        return collision_module.CollisionCache(path), entries

    def test_reads_legacy_v9_tokens_without_stride_drift(self):
        cache, entries = self._parse(9)

        self.assertEqual(cache.Version, 9)
        self.assertEqual(cache.ReadBufferSize, READ_BUFFER_SIZE)
        self.assertEqual(cache.LoadBufferSize, LOAD_BUFFER_SIZE)
        self.assertEqual(cache.BufferSize, (LOAD_BUFFER_SIZE << 32) | READ_BUFFER_SIZE)
        self.assertEqual([item.Name for item in cache.Files], [entry[0] for entry in entries])
        self.assertEqual(cache.Files[1].Unk1, 101)
        self.assertEqual(cache.Files[1].Unk2, 201)
        self.assertEqual(cache.Files[1].Unk3, 301)
        self.assertEqual(cache.Files[1].unk5, b"Q" * 16)
        output = io.BytesIO()
        cache.Files[1].Extract(output)
        self.assertEqual(output.getvalue(), entries[1][1])

    def test_keeps_preexisting_v1_and_v2_layouts(self):
        for version in (1, 2):
            with self.subTest(version=version):
                cache, entries = self._parse(version)
                self.assertEqual([item.Name for item in cache.Files], [entry[0] for entry in entries])
                output = io.BytesIO()
                cache.Files[0].Extract(output)
                self.assertEqual(output.getvalue(), entries[0][1])

    def test_reads_v10_and_v11_64_byte_tokens_and_extracts(self):
        for version in (10, 11):
            with self.subTest(version=version):
                cache, entries = self._parse(version)

                self.assertEqual(cache.Version, version)
                self.assertEqual([item.Name for item in cache.Files], [entry[0] for entry in entries])
                self.assertEqual(cache.Files[1].Unk1, 0x100000001)
                self.assertEqual(cache.Files[1].Unk2, 0x200000001)
                self.assertEqual(cache.Files[1].Unk3, 0x300000001)
                self.assertEqual(struct.unpack("<4f", cache.Files[1].unk4), (-2.0, -3.0, 2.0, 3.0))
                self.assertEqual(cache.Files[1].Tail, b"\0" * 3)
                output = io.BytesIO()
                cache.Files[1].Extract(output)
                self.assertEqual(output.getvalue(), entries[1][1])

    def test_rejects_unsupported_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "collision.cache"
            path.write_bytes(b"CC3W" + struct.pack("<I", 12) + bytes(8))
            with self.assertRaisesRegex(
                collision_module.InvalidCollisionCacheException,
                "Unsupported collision cache version 12",
            ):
                collision_module.CollisionCache(path)


if __name__ == "__main__":
    unittest.main()
