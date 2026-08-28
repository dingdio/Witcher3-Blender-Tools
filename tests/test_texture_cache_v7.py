import importlib.util
import struct
import sys
import tempfile
import types
import unittest
import zlib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CR2W_DIR = REPO_ROOT / "witcher3_tools" / "CR2W"
TEXTURE_CACHE_DIR = CR2W_DIR / "witcher_cache" / "TextureCache"
PACKAGE_NAME = "_texture_cache_v7_test"
MAGIC = 1415070536
PAGE = 4096


def _stub_package(name, path):
    package = types.ModuleType(name)
    package.__path__ = [str(path)]
    sys.modules[name] = package


# Stub the packages so relative imports resolve without running TextureCache/__init__.py (bpy-side managers).
_stub_package(PACKAGE_NAME, CR2W_DIR)
_stub_package(f"{PACKAGE_NAME}.witcher_cache", CR2W_DIR / "witcher_cache")
_stub_package(f"{PACKAGE_NAME}.witcher_cache.TextureCache", TEXTURE_CACHE_DIR)
_spec = importlib.util.spec_from_file_location(
    f"{PACKAGE_NAME}.witcher_cache.TextureCache.TextureCache", TEXTURE_CACHE_DIR / "TextureCache.py")
texture_cache_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = texture_cache_module
_spec.loader.exec_module(texture_cache_module)
bstream_module = sys.modules[f"{PACKAGE_NAME}.bStream"]

# (name, width, mip payloads base-first) — BC1 sizes for 8x8, 4x4, 2x2.
ENTRIES = (
    ("characters\\a.xbm", 8, (b"A" * 32, b"a" * 8, b"@" * 8)),
    ("dlc\\bob\\b.xbm", 4, (b"B" * 8,)),
)


def _write_cache(path, version, entries=ENTRIES, magic=MAGIC):
    body = bytearray()
    rows = []
    names = bytearray()
    mip_offsets = []
    for name, width, payloads in entries:
        body.extend(bytes(-len(body) % PAGE))
        page = len(body) // PAGE
        base, mips = payloads[0], payloads[1:]
        zbase = zlib.compress(base)
        body += struct.pack("<IIB", len(zbase), len(base), 0) + zbase
        for index, mip in enumerate(mips, 1):
            zmip = zlib.compress(mip)
            body += struct.pack("<IIB", len(zmip), len(mip), index) + zmip
        num_mips = len(mips) | (1 << 16 if version >= 7 else 0)  # v7 packs a flag into the upper 16 bits
        rows.append(struct.pack(
            "<IiIIIIhhhhiiqBBBB",
            zlib.crc32(name.encode()), len(names), page, len(zbase), len(base), PAGE,
            width, width, len(payloads), 1, len(mip_offsets), num_mips, 0, 0x07, 0, 0, 0,
        ))
        mip_offsets.extend([0] * len(mips))
        names += name.encode() + b"\0"
    footer = struct.pack(
        "QIIIIII", 0, len(body) // PAGE, len(entries), len(names), len(mip_offsets), magic, version)
    path.write_bytes(bytes(body) + struct.pack(f"<{len(mip_offsets)}I", *mip_offsets) + bytes(names) + b"".join(rows) + footer)


class TextureCacheV7Tests(unittest.TestCase):
    def test_reads_version_6_and_7_entry_tables_and_mip_chains(self):
        for version in (6, 7):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "texture.cache"
                _write_cache(path, version)
                cache = texture_cache_module.TextureCache(str(path))
                self.assertEqual(cache.version, version)
                self.assertEqual(cache.Names, [entry[0] for entry in ENTRIES])
                raw = path.read_bytes()
                for item, (name, width, payloads) in zip(cache.Files, ENTRIES):
                    self.assertEqual(item.Name, name)
                    self.assertEqual((item.BaseWidth, item.BaseHeight, item.Mipcount), (width, width, len(payloads)))
                    self.assertEqual(item.Format, texture_cache_module.CommonImageTools.get_eformat_from_redengine_byte(0x07))
                    self.assertEqual(len(item.MipMapInfo), len(payloads) - 1)
                    base_start = item.PageOffset * PAGE + 9
                    self.assertEqual(zlib.decompress(raw[base_start:base_start + item.ZSize]), payloads[0])
                    for info, mip in zip(item.MipMapInfo, payloads[1:]):
                        self.assertEqual(zlib.decompress(raw[info.Offset:info.Offset + info.ZSize]), mip)

    def test_extract_writes_dds_with_every_mip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "texture.cache"
            _write_cache(path, 7)
            cache = texture_cache_module.TextureCache(str(path))
            output = bstream_module.bStream(data=b"")
            cache.Files[0].Extract(output)
            data = output.fhandle.getvalue()
            self.assertEqual(data[:4], b"DDS ")
            self.assertTrue(data.endswith(b"".join(ENTRIES[0][2])))

    def test_rejects_wrong_magic(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "texture.cache"
            _write_cache(path, 7, magic=MAGIC + 1)
            with self.assertRaises(Exception):
                texture_cache_module.TextureCache(str(path))


if __name__ == "__main__":
    unittest.main()
