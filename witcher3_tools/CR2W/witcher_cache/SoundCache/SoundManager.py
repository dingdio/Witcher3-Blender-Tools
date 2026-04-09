import hashlib
import logging
import os
import pickle
import time
import re
from collections import OrderedDict
from typing import Dict, List, Optional

from ..common_cache.WitcherArchiveManager import (
    Configuration,
    WitcherArchiveManager,
    has_game_content_root,
    normalize_game_path,
    refresh_game_configuration_path,
)
from ..Cache import Cache
from .. import cache_meta
from ....extension_paths import get_cache_root
from .SoundBanksInfo import SoundBanksInfoXML
from .SoundCache import SoundCache
from .SoundCacheItem import SoundCacheItem


log = logging.getLogger(__name__)


def natural_sort_key(value: str):
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r"(\d+)", value)]


def _soundbanks_xml_path() -> str:
    return os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "..", "data", "soundbanksinfo.xml")
    )


def _soundbanks_compact_json_path() -> str:
    return os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "..", "data", "soundbanksinfo.json.gz")
    )


def _soundbanks_json_path() -> str:
    return os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "..", "data", "soundbanksinfo.json")
    )


def _soundbanks_metadata_path() -> str:
    candidates = (
        _soundbanks_compact_json_path(),
        _soundbanks_json_path(),
        _soundbanks_xml_path(),
    )
    for path in candidates:
        if os.path.exists(path):
            return path
    return candidates[0]


def _soundbanks_metadata_token() -> str:
    path = _soundbanks_metadata_path()
    if not os.path.exists(path):
        return "missing"
    try:
        return f"{int(os.path.getmtime(path))}:{os.path.getsize(path)}"
    except Exception:
        return "unknown"


class SoundManager(WitcherArchiveManager):
    InstanceManager = None
    InstanceManagerMods = None
    CACHE_FILENAME = "sound_cache.pkl"
    CACHE_FILENAME_MODS = "sound_cache_mods.pkl"

    def __init__(self):
        self.base_path: Optional[str] = None
        self.cache_files: List[str] = []
        self.Items: Dict[str, List[SoundCacheItem]] = OrderedDict()
        self.Archives: Dict[str, SoundCache] = {}
        self.FileList: List[SoundCacheItem] = []
        self.Extensions: List[str] = []
        self.AutocompleteSource: List[str] = []
        self.soundBanksInfo = SoundBanksInfoXML(_soundbanks_metadata_path())

    @property
    def TypeName(self):
        return "SoundCache"

    def find_item_by_path_name(self, filepath: str) -> Optional[List[SoundCacheItem]]:
        return self.Items.get(filepath, None)

    def LoadModBundle(self, filename: str):
        if filename in self.Archives:
            return

        try:
            bundle = SoundCache(filename, soundbanks_info=self.soundBanksInfo)
        except Exception as exc:
            log.warning("Failed to load sound cache %s: %s", filename, exc)
            return

        mod_folder = WitcherArchiveManager.GetModFolder(filename)
        for item in bundle.Files:
            key = f"{mod_folder}\\{item.Name}"
            if key not in self.Items:
                self.Items[key] = []
            self.Items[key].append(item)
            self.FileList.append(item)

        self.Archives[filename] = bundle
        self.cache_files.append(filename)

    def LoadBundle(self, filename: str, ispatch: bool = False):
        if filename in self.Archives:
            return

        try:
            bundle = SoundCache(filename, soundbanks_info=self.soundBanksInfo)
        except Exception as exc:
            log.warning("Failed to load sound cache %s: %s", filename, exc)
            return

        for item in bundle.Files:
            if item.Name not in self.Items:
                self.Items[item.Name] = []
            self.Items[item.Name].append(item)
            self.FileList.append(item)

        self.Archives[filename] = bundle
        self.cache_files.append(filename)

    def LoadAll(self, base_path: str):
        self.base_path = normalize_game_path(base_path)
        self.cache_files = []

        if not has_game_content_root(self.base_path):
            log.info("Sound cache skipped: Witcher 3 path not set or invalid: %s", self.base_path or "<unset>")
            return

        content = os.path.join(self.base_path, "content")
        dlc = os.path.join(self.base_path, "dlc")

        if os.path.isdir(content):
            content_dirs = [
                d for d in os.listdir(content)
                if os.path.isdir(os.path.join(content, d)) and d.startswith("content")
            ]
            patch_dirs = [
                d for d in os.listdir(content)
                if os.path.isdir(os.path.join(content, d)) and d.startswith("patch")
            ]
            content_dirs.sort(key=natural_sort_key)
            patch_dirs.sort(key=natural_sort_key)

            for dir_name in content_dirs + patch_dirs:
                dir_path = os.path.join(content, dir_name)
                for root, _dirs, files in os.walk(dir_path):
                    for file_name in files:
                        filepath = os.path.join(root, file_name)
                        if file_name.endswith(".cache") and Cache.GetCacheTypeOfFile(filepath) == Cache.Cachetype.Sound:
                            self.LoadBundle(filepath)

        if os.path.isdir(dlc):
            dlc_dirs = [
                os.path.join(dlc, entry) for entry in os.listdir(dlc)
                if os.path.isdir(os.path.join(dlc, entry))
            ]
            dlc_dirs.sort(key=natural_sort_key)

            for dir_path in dlc_dirs:
                dlc_name = os.path.basename(dir_path).lower()
                if dlc_name not in self.VANILLA_DLC_LIST:
                    continue
                for root, _dirs, files in os.walk(dir_path):
                    for file_name in sorted(files):
                        filepath = os.path.join(root, file_name)
                        if file_name.endswith(".cache") and Cache.GetCacheTypeOfFile(filepath) == Cache.Cachetype.Sound:
                            self.LoadBundle(filepath)

    def LoadModsBundles(self, mods_path: str, dlc_path: str):
        self.base_path = normalize_game_path(Configuration.ExecutablePath)
        self.cache_files = []

        if not has_game_content_root(self.base_path):
            log.info("Sound cache skipped (mods): Witcher 3 path not set or invalid: %s", self.base_path or "<unset>")
            return
        if not mods_path:
            return
        if not os.path.exists(mods_path):
            os.makedirs(mods_path, exist_ok=True)

        mods_dirs = [
            os.path.join(mods_path, entry) for entry in os.listdir(mods_path)
            if os.path.isdir(os.path.join(mods_path, entry))
        ]
        mods_dirs.sort(key=natural_sort_key)

        for dir_path in mods_dirs:
            for root, _dirs, files in os.walk(dir_path):
                for file_name in files:
                    filepath = os.path.join(root, file_name)
                    if file_name.endswith(".cache") and Cache.GetCacheTypeOfFile(filepath) == Cache.Cachetype.Sound:
                        self.LoadModBundle(filepath)

        if os.path.isdir(dlc_path):
            dlc_dirs = [
                os.path.join(dlc_path, entry) for entry in os.listdir(dlc_path)
                if os.path.isdir(os.path.join(dlc_path, entry))
            ]
            dlc_dirs.sort(key=natural_sort_key)

            for dir_path in dlc_dirs:
                dlc_name = os.path.basename(dir_path).lower()
                if dlc_name in self.VANILLA_DLC_LIST:
                    continue
                for root, _dirs, files in os.walk(dir_path):
                    for file_name in sorted(files):
                        filepath = os.path.join(root, file_name)
                        if file_name.endswith(".cache") and Cache.GetCacheTypeOfFile(filepath) == Cache.Cachetype.Sound:
                            self.LoadModBundle(filepath)

    def OpenFile(self):
        pass

    def GetString(self):
        pass

    @classmethod
    def from_json(cls, data):
        pass

    @staticmethod
    def Get(do_reload: bool = False, loadmods: bool = False) -> "SoundManager":
        current_base_path = refresh_game_configuration_path()
        instance_manager = SoundManager.InstanceManagerMods if loadmods else SoundManager.InstanceManager
        cache_name = SoundManager.CACHE_FILENAME_MODS if loadmods else SoundManager.CACHE_FILENAME

        if (
            instance_manager is not None
            and getattr(instance_manager, "base_path", None) != current_base_path
        ):
            do_reload = True

        if instance_manager is None or do_reload:
            cache_root = get_cache_root(create=True)
            cache_dir = os.path.join(cache_root, "SoundCache")
            os.makedirs(cache_dir, exist_ok=True)
            filename = os.path.join(cache_dir, cache_name)
            meta_path = cache_meta.get_meta_path(filename)
            start_time = time.time()

            def load_from_game(cache_filename: str) -> "SoundManager":
                manager = SoundManager()
                manager.base_path = current_base_path
                if loadmods:
                    manager.LoadModsBundles(Configuration.GameModDir, Configuration.GameDlcDir)
                else:
                    manager.LoadAll(current_base_path)

                if not has_game_content_root(current_base_path):
                    return manager

                try:
                    with open(cache_filename, "wb") as handle:
                        pickle.dump(manager, handle, protocol=pickle.HIGHEST_PROTOCOL)
                except Exception as exc:
                    log.warning("Failed to save sound cache: %s", exc)

                signature, source = SoundManager.BuildSourceSignature(loadmods)
                meta = cache_meta.make_meta(cache_name, cache_filename, signature, source)
                cache_meta.save_meta(meta_path, meta)
                return manager

            if not has_game_content_root(current_base_path):
                manager = load_from_game(filename)
            elif not os.path.exists(filename) or do_reload:
                manager = load_from_game(filename)
            else:
                meta = cache_meta.load_meta(meta_path)
                current_sig, _ = SoundManager.BuildSourceSignature(loadmods)
                if not cache_meta.signatures_match(meta.get("signature", {}), current_sig):
                    log.info("Sound cache stale, rebuilding %s...", "mods" if loadmods else "vanilla")
                    manager = load_from_game(filename)
                else:
                    try:
                        with open(filename, "rb") as handle:
                            manager = pickle.load(handle)
                        manager.base_path = current_base_path
                        soundbanks_info = getattr(manager, "soundBanksInfo", None)
                        if (
                            soundbanks_info is None
                            or not hasattr(soundbanks_info, "resolve_event_name")
                            or not hasattr(soundbanks_info, "EventsByName")
                        ):
                            manager.soundBanksInfo = SoundBanksInfoXML(_soundbanks_metadata_path())
                    except Exception as exc:
                        log.warning("Failed to load cached sound data, rebuilding: %s", exc)
                        manager = load_from_game(filename)

            time_taken = time.time() - start_time
            log.info("Loaded Sound Cache in %.2f seconds (%d files)", time_taken, len(manager.FileList))
            if loadmods:
                SoundManager.InstanceManagerMods = manager
            else:
                SoundManager.InstanceManager = manager
            instance_manager = manager

        return instance_manager

    @staticmethod
    def BuildSourceSignature(loadmods: bool = False):
        base_path = refresh_game_configuration_path()
        if loadmods:
            roots = cache_meta.get_mod_dirs(os.path.join(base_path, "mods"))
            dlc_dirs = cache_meta.get_dlc_dirs(base_path, vanilla_only=False, vanilla_list=WitcherArchiveManager.VANILLA_DLC_LIST)
            vanilla_set = {value.lower() for value in WitcherArchiveManager.VANILLA_DLC_LIST}
            roots.extend([path for path in dlc_dirs if os.path.basename(path).lower() not in vanilla_set])
        else:
            roots = cache_meta.get_content_patch_dirs(base_path)
            roots.extend(cache_meta.get_dlc_dirs(base_path, vanilla_only=True, vanilla_list=WitcherArchiveManager.VANILLA_DLC_LIST))

        def _predicate(path: str) -> bool:
            if not path.lower().endswith(".cache"):
                return False
            return Cache.GetCacheTypeOfFile(path) == Cache.Cachetype.Sound

        signature = cache_meta.compute_signature(cache_meta.iter_files(roots, _predicate))
        metadata_token = _soundbanks_metadata_token()
        mix = f"{signature.get('hash', '')}|soundbanks:{metadata_token}"
        signature["hash"] = hashlib.sha1(mix.encode("utf-8", "ignore")).hexdigest()
        metadata_path = _soundbanks_metadata_path()
        source = {
            "type": "sound_cache_mods" if loadmods else "sound_cache",
            "base_path": base_path,
            "roots": roots,
            "soundbanks_metadata": metadata_path,
            "soundbanks_xml": _soundbanks_xml_path(),
            "soundbanks_token": metadata_token,
        }
        return signature, source

    @staticmethod
    def ResetInstance():
        SoundManager.InstanceManager = None
        SoundManager.InstanceManagerMods = None

    def __repr__(self) -> str:
        return f"SoundManager({len(self.Archives)} archives, {len(self.FileList)} files)"
