import csv
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
BUNDLES_DIR = REPO_ROOT / "witcher3_tools" / "CR2W" / "witcher_cache" / "Bundles"
WITCHER_CACHE_DIR = BUNDLES_DIR.parent
PACKAGE_NAME = "_bundle_v5_test"


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


package = types.ModuleType(PACKAGE_NAME)
package.__path__ = [str(BUNDLES_DIR)]
sys.modules[PACKAGE_NAME] = package
_load_module(f"{PACKAGE_NAME}.BundleItem", BUNDLES_DIR / "BundleItem.py")
bundle_module = _load_module(f"{PACKAGE_NAME}.Bundle", BUNDLES_DIR / "Bundle.py")

pathhash_package = types.ModuleType("_bundle_pathhash_test")
pathhash_package.__path__ = [str(WITCHER_CACHE_DIR.parent)]
sys.modules[pathhash_package.__name__] = pathhash_package
pathhash_cache_package = types.ModuleType("_bundle_pathhash_test.witcher_cache")
pathhash_cache_package.__path__ = [str(WITCHER_CACHE_DIR)]
sys.modules[pathhash_cache_package.__name__] = pathhash_cache_package
common_blender = types.ModuleType("_bundle_pathhash_test.common_blender")
common_blender.get_game_path = lambda: ""
sys.modules[common_blender.__name__] = common_blender
pathhash_module = _load_module(
    "_bundle_pathhash_test.witcher_cache.bundle",
    WITCHER_CACHE_DIR / "bundle.py",
)


def _preamble(header_version, header_size, file_size):
    result = bytearray(32)
    result[:8] = b"POTATO70"
    struct.pack_into("<IIIH", result, 8, file_size, 0, header_size, header_version)
    return result


def _name_bytes(name):
    return name.encode("ascii").ljust(256, b"\0")


class BundleV5Tests(unittest.TestCase):
    def test_reads_two_v5_entries_without_stride_drift_and_extracts(self):
        names = ("gameplay\\first.w2ent", "gameplay\\second.w2ent")
        payloads = (b"CR2W" + b"first" * 40, b"CR2W" + b"second" * 40)
        packed = (zlib.compress(payloads[0]), payloads[1])
        header_size = 304 * 2
        offsets = (32 + header_size, 32 + header_size + len(packed[0]))

        rows = []
        for name, payload, stored, offset, compression in zip(names, payloads, packed, offsets, (1, 0)):
            rows.append(
                _name_bytes(name)
                + bytes(16)
                + struct.pack(
                    "<QIIIB11x",
                    offset,
                    len(payload),
                    len(stored),
                    zlib.crc32(payload),
                    compression,
                )
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            game_root = Path(temp_dir)
            path = game_root / "content" / "base" / "bundles" / "test.bundle"
            path.parent.mkdir(parents=True)
            file_size = 32 + header_size + sum(map(len, packed))
            path.write_bytes(_preamble(5, header_size, file_size) + b"".join(rows) + b"".join(packed))

            bundle = bundle_module.Bundle(path)
            self.assertEqual(list(bundle.Items), list(names))
            self.assertEqual(list(pathhash_module.hash_bundle_paths(path)), list(names))
            csv_path = game_root / "pathhashes.csv"
            pathhash_module.create_pathhashes(str(game_root), str(csv_path))
            self.assertFalse(pathhash_module.ensure_pathhashes(str(game_root), str(csv_path)))
            with csv_path.open(newline="") as csv_file:
                self.assertEqual([row["Path"] for row in csv.DictReader(csv_file)], list(names))
            path.write_bytes(path.read_bytes() + b"\0")
            self.assertTrue(pathhash_module.ensure_pathhashes(str(game_root), str(csv_path)))
            for name, payload in zip(names, payloads):
                output = io.BytesIO()
                bundle.Items[name].extract(output)
                self.assertEqual(output.getvalue(), payload)

    def test_keeps_legacy_v3_layout(self):
        name = "gameplay\\legacy.w2ent"
        payload = b"CR2Wlegacy"
        header_size = 320
        offset = 32 + header_size
        date = (2026 << 20) | (7 << 15) | (30 << 10)
        time = (19 << 22) | (15 << 16) | (10 << 10)
        row = (
            _name_bytes(name)
            + bytes(16)
            + struct.pack("<6I", 0, len(payload), len(payload), offset, date, time)
            + bytes(16)
            + struct.pack("<2I", zlib.crc32(payload), 0)
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "v3.bundle"
            path.write_bytes(_preamble(3, header_size, offset + len(payload)) + row + payload)

            item = bundle_module.Bundle(path).Items[name]
            self.assertEqual(item.page_offset, offset)
            self.assertEqual(item.date_string, "30/7/2026 19:15:10")


if __name__ == "__main__":
    unittest.main()
