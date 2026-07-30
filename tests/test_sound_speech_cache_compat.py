import importlib.util
import io
import struct
import sys
import tempfile
import types
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CR2W_DIR = REPO_ROOT / "witcher3_tools" / "CR2W"
CACHE_DIR = CR2W_DIR / "witcher_cache"
PACKAGE_NAME = "_sound_speech_cache_test"


def _package(name, path):
    package = types.ModuleType(name)
    package.__path__ = [str(path)]
    sys.modules[name] = package


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_package(PACKAGE_NAME, CR2W_DIR.parent)
_package(f"{PACKAGE_NAME}.CR2W", CR2W_DIR)
_package(f"{PACKAGE_NAME}.CR2W.witcher_cache", CACHE_DIR)
_package(f"{PACKAGE_NAME}.CR2W.witcher_cache.SoundCache", CACHE_DIR / "SoundCache")
_package(f"{PACKAGE_NAME}.CR2W.witcher_cache.Speech", CACHE_DIR / "Speech")
_package(f"{PACKAGE_NAME}.CR2W.witcher_cache.common_cache", CACHE_DIR / "common_cache")
_load_module(f"{PACKAGE_NAME}.CR2W.bStream", CR2W_DIR / "bStream.py")
_load_module(f"{PACKAGE_NAME}.CR2W.bin_helpers", CR2W_DIR / "bin_helpers.py")
_load_module(
    f"{PACKAGE_NAME}.CR2W.witcher_cache.SoundCache.SoundCacheItem",
    CACHE_DIR / "SoundCache" / "SoundCacheItem.py",
)
_load_module(
    f"{PACKAGE_NAME}.CR2W.witcher_cache.SoundCache.SoundBanksInfo",
    CACHE_DIR / "SoundCache" / "SoundBanksInfo.py",
)

blender_common = types.ModuleType(f"{PACKAGE_NAME}.CR2W.witcher_cache.blender_common")
blender_common.get_game_path = lambda: ""
blender_common.get_W3_VOICE_PATH = lambda: ""
sys.modules[blender_common.__name__] = blender_common

extension_paths = types.ModuleType(f"{PACKAGE_NAME}.extension_paths")
extension_paths.get_cache_root = lambda create=False: ""
sys.modules[extension_paths.__name__] = extension_paths

dialog_language = types.ModuleType(f"{PACKAGE_NAME}.dialog_language")
dialog_language.normalize_dialog_language = lambda language: str(language or "en").lower()
dialog_language.get_active_voice_language = lambda: "en"
sys.modules[dialog_language.__name__] = dialog_language

sound_module = _load_module(
    f"{PACKAGE_NAME}.CR2W.witcher_cache.SoundCache.SoundCache",
    CACHE_DIR / "SoundCache" / "SoundCache.py",
)
speech_module = _load_module(
    f"{PACKAGE_NAME}.CR2W.witcher_cache.Speech.W3Speech",
    CACHE_DIR / "Speech" / "W3Speech.py",
)
cache_meta_module = _load_module(
    f"{PACKAGE_NAME}.CR2W.witcher_cache.cache_meta",
    CACHE_DIR / "cache_meta.py",
)
archive_manager_module = _load_module(
    f"{PACKAGE_NAME}.CR2W.witcher_cache.common_cache.WitcherArchiveManager",
    CACHE_DIR / "common_cache" / "WitcherArchiveManager.py",
)
speech_manager_module = _load_module(
    f"{PACKAGE_NAME}.CR2W.witcher_cache.Speech.SpeechManager",
    CACHE_DIR / "Speech" / "SpeechManager.py",
)


def _sound_cache_bytes(version):
    payload = b"wem"
    name = b"voices/test.wem\0"
    header_size = 64 if version >= 2 else 48
    data_offset = header_size
    names_offset = data_offset + len(payload)
    info_offset = names_offset + len(name)
    raw_header = struct.pack("<4sIII", b"CS3W", version, 0, 0)

    if version >= 2:
        index_header = struct.pack(
            "<QI4sQI4sQQ",
            info_offset,
            1,
            b"\xfb\x01\0\0",
            names_offset,
            len(name),
            b"\xfc\x01\0\0",
            4096,
            0,
        )
        token = struct.pack("<I4sQQ", 0, b"\xfd\x01\0\0", data_offset, len(payload))
    else:
        index_header = struct.pack(
            "<IIIII4sQ",
            info_offset,
            1,
            names_offset,
            len(name),
            4096,
            b"\xfb\x01\0\0",
            0,
        )
        token = struct.pack("<III", 0, data_offset, len(payload))

    return raw_header + index_header + payload + name + token


def _speech_bytes(version, include_placeholders):
    wem = b"WEMDATA"
    cr2w = b"CR2W"
    valid_id = 0x12345678
    item_count = 3 if include_placeholders else 1
    header_size = 4 + 4 + 2 + 1 + item_count * 40 + 2
    cr2w_offset = header_size + 12 + len(wem)
    valid = (valid_id, 7, header_size, 0, len(wem) + 12, 0, cr2w_offset, 0, len(cr2w), 0)
    rows = [valid]
    if include_placeholders:
        rows = [
            (0x11111111, 0, 0, 0, 0, 0, cr2w_offset, 0, len(cr2w), 0),
            (0x22222222, 0, 0, 0, 0, 0, 0, 0, 0, 0),
            valid,
        ]

    return (
        struct.pack("<4sIH", b"CPSW", version, 0x4397)
        + bytes([item_count])
        + b"".join(struct.pack("<10I", *row) for row in rows)
        + struct.pack("<H", 0x5139)
        + struct.pack("<I", len(wem))
        + wem
        + struct.pack("<fI", 1.25, 4)
        + cr2w
    )


class SoundCacheCompatibilityTests(unittest.TestCase):
    def test_reads_native_v2_alignment_and_legacy_v1(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            for version in (1, 2):
                path = Path(temp_dir) / f"v{version}.cache"
                path.write_bytes(_sound_cache_bytes(version))
                cache = sound_module.SoundCache(str(path))

                self.assertEqual(cache.NumberOfFiles, 1)
                self.assertEqual(cache.Files[0].RawName, "voices\\test.wem")
                output = io.BytesIO()
                cache.Files[0].Extract(output)
                self.assertEqual(output.getvalue(), b"wem")


class W3SpeechCompatibilityTests(unittest.TestCase):
    def test_skips_empty_wem_placeholders_in_newer_layout(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "testpc.w3speech"
            path.write_bytes(_speech_bytes(164, include_placeholders=True))
            speech = speech_module.W3Speech(str(path))

            self.assertEqual(speech.version, 164)
            self.assertEqual(len(speech.item_infos), 1)
            self.assertEqual(speech.item_infos[0].id, 0x12345678 ^ 0x79321793)
            self.assertAlmostEqual(speech.item_infos[0].duration, 1.25)

    def test_keeps_v163_speech_layout(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "enpc.w3speech"
            path.write_bytes(_speech_bytes(163, include_placeholders=False))
            speech = speech_module.W3Speech(str(path))

            self.assertEqual(speech.version, 163)
            self.assertEqual(len(speech.item_infos), 1)
            self.assertAlmostEqual(speech.item_infos[0].duration, 1.25)

    def test_manager_saves_the_same_versioned_signature_it_validates(self):
        manager_type = speech_manager_module.SpeechManager
        with tempfile.TemporaryDirectory() as temp_dir:
            game_root = Path(temp_dir) / "game"
            (game_root / "content").mkdir(parents=True)
            cache_root = Path(temp_dir) / "cache"
            speech_manager_module.get_cache_root = lambda create=False: str(cache_root)
            archive_manager_module.set_forced_game_path(str(game_root))
            manager_type.InstanceManager = None
            manager_type.InstanceManagers.clear()
            try:
                self.assertEqual(
                    Path(manager_type.GetCachePath("en", create=False)),
                    cache_root / "Speech" / "speech_cache_en.pkl",
                )
                manager_type.Get(do_reload=True, language="en")
                cache_path = cache_root / "Speech" / "speech_cache_en.pkl"
                meta = cache_meta_module.load_meta(str(cache_path) + ".meta.json")
                current_signature, _ = manager_type.BuildSourceSignature("en")
                self.assertTrue(
                    cache_meta_module.signatures_match(meta["signature"], current_signature)
                )

                manager_type.InstanceManager = None
                manager_type.InstanceManagers.clear()
                original_load_all = manager_type.LoadAll
                manager_type.LoadAll = lambda *args, **kwargs: self.fail(
                    "matching cache metadata should not rebuild"
                )
                try:
                    manager_type.Get(language="en")
                finally:
                    manager_type.LoadAll = original_load_all
            finally:
                archive_manager_module.set_forced_game_path(None)
                manager_type.InstanceManager = None
                manager_type.InstanceManagers.clear()


if __name__ == "__main__":
    unittest.main()
