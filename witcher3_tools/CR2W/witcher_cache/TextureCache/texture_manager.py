import os
import time
import json
import re
import glob
from pathlib import Path
from ..blender_common import get_game_path
from ..Cache import Cache
from ..common_cache.WitcherArchiveManager import WitcherArchiveManager, Configuration
from .. import cache_meta
from ....extension_paths import get_cache_root
from .TextureCache import TextureCache
import pickle
import gzip
import logging
log = logging.getLogger(__name__)

def natural_sort_key(s):
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s)]


def _normalize_game_path(path: str) -> str:
    if not path:
        return ""
    try:
        return os.path.normpath(os.path.abspath(path))
    except Exception:
        return os.path.normpath(path)


def _refresh_texture_configuration_path() -> str:
    base_path = _normalize_game_path(get_game_path())
    Configuration.ExecutablePath = base_path
    Configuration.GameModDir = os.path.join(base_path, "mods") if base_path else ""
    Configuration.GameDlcDir = os.path.join(base_path, "dlc") if base_path else ""
    return base_path


def _has_texture_source_root(base_path: str) -> bool:
    if not base_path:
        return False
    return os.path.isdir(os.path.join(base_path, "content"))


class TextureManager():
    InstanceManager = None
    InstanceManagerMods = None
    VANILLA_DLC_LIST = [
        "dlc1", "dlc2", "dlc3", "dlc4", "dlc5", "dlc6", "dlc7", "dlc8",
        "dlc9", "dlc10", "dlc11", "dlc12", "dlc13", "dlc14", "dlc15",
        "dlc16", "dlc17", "dlc18", "dlc20", "bob", "ep1"
    ]
    def __init__(self):
        self.base_path = None
        self.cache_files = None
        
        self.Items = {}  # Dictionary for string to list of IWitcherFile
        self.Archives = {}  # Dictionary for string to TextureCache
        self.FileList = []  # List of IWitcherFile objects
        self.HashDict = {}

        self.Extensions = []  # List of strings
        self.AutocompleteSource = []  # This can be a list in Python

        
        # Items = new Dictionary<string, List<IWitcherFile>>();
        # Archives = new Dictionary<string, TextureCache>();
        # FileList = new List<IWitcherFile>();

        # Extensions = new List<string>();
        # AutocompleteSource = new AutoCompleteStringCollection();

    # def find_item_by_hash(self, hash_value):
    #     for key in self.Items:
    #         for item in self.Items[key]:
    #             if item.Hash == hash_value:
    #                 return item
    #     return None
    
    def find_item_by_hash(self, hash_value):
        return self.HashDict.get(hash_value, None)
    
    def find_item_by_path_name(self, filePath):
        return self.Items.get(filePath, None)
    
    def LoadModBundle(self, filename):
        if filename in self.Archives:
            return

        try:
            bundle = TextureCache(filename)
        except Exception as exc:
            log.warning("Failed to load texture cache %s: %s", filename, exc)
            return

        if self.cache_files is None:
            self.cache_files = []
        self.cache_files.append(filename)
        for item in bundle.Files:
            mod_folder = WitcherArchiveManager.GetModFolder(filename) + "\\" + item.Name
            if mod_folder not in self.Items:
                self.Items[mod_folder] = []
            self.Items[mod_folder].append(item)

            if item.Hash not in self.HashDict:
                self.HashDict[item.Hash] = []
            self.HashDict[item.Hash].append(item)
        self.Archives[filename] = bundle

    def LoadModsBundles(self, mods_path, dlc_path):
        """Load texture caches from mod directories."""
        self.base_path = _normalize_game_path(Configuration.ExecutablePath)
        if not mods_path:
            return
        if not os.path.exists(mods_path):
            os.makedirs(mods_path, exist_ok=True)

        mods_dirs = sorted(glob.glob(os.path.join(mods_path, '*')))
        for dir_path in mods_dirs:
            if not os.path.isdir(dir_path):
                continue
            for root, dirs, files in os.walk(dir_path):
                for file in files:
                    filepath = os.path.join(root, file)
                    if file.endswith('.cache') and Cache.GetCacheTypeOfFile(filepath) == Cache.Cachetype.Texture:
                        self.LoadModBundle(filepath)

        # Load non-vanilla DLCs (modded DLCs)
        if os.path.exists(dlc_path):
            dlc_dirs = sorted(glob.glob(os.path.join(dlc_path, '*')))
            for dir_path in dlc_dirs:
                if not os.path.isdir(dir_path):
                    continue
                dlc_name = os.path.basename(dir_path).lower()
                if dlc_name not in self.VANILLA_DLC_LIST:
                    for root, dirs, files in os.walk(dir_path):
                        for file in sorted(files):
                            filepath = os.path.join(root, file)
                            if file.endswith('.cache') and Cache.GetCacheTypeOfFile(filepath) == Cache.Cachetype.Texture:
                                self.LoadModBundle(filepath)

    def LoadBundle(self, filename):
        if filename in self.Archives:
            return

        try:
            bundle = TextureCache(filename)
        except Exception as exc:
            log.warning("Failed to load texture cache %s: %s", filename, exc)
            return

        for item in bundle.Files:
            if item.Name not in self.Items:
                self.Items[item.Name] = []
            self.Items[item.Name].append(item)
            
            if item.Hash not in self.HashDict:
                self.HashDict[item.Hash] = []
            self.HashDict[item.Hash].append(item)
        self.Archives[filename] = bundle

    def LoadAll(self, base_path):
        self.base_path = _normalize_game_path(base_path)
        self.cache_files = []

        if not _has_texture_source_root(self.base_path):
            log.info("Texture cache skipped: Witcher 3 path not set or invalid: %s", self.base_path or "<unset>")
            return

        content = os.path.join(self.base_path, "content")
        dlc = os.path.join(self.base_path, "dlc")
        content_dirs = [d for d in os.listdir(content) if os.path.isdir(os.path.join(content, d)) and d.startswith("content")]
        content_dirs.sort(key=natural_sort_key)
        patch_dirs = [d for d in os.listdir(content) if os.path.isdir(os.path.join(content, d)) and d.startswith("patch")]
        patch_dirs.sort(key=natural_sort_key)

        for dir_name in content_dirs + patch_dirs:
            dir_path = os.path.join(content, dir_name)
            for root, dirs, files in os.walk(dir_path):
                for file in files:
                    if file.endswith('.cache') and Cache.GetCacheTypeOfFile(os.path.join(root, file)) == Cache.Cachetype.Texture:
                        self.LoadBundle(os.path.join(root, file))


        if os.path.exists(dlc):
            dlc_dirs = [os.path.join(dlc, d) for d in os.listdir(dlc) if os.path.isdir(os.path.join(dlc, d))]
            dlc_dirs.sort(key=natural_sort_key)
            vanilla_dlc_names = {name.lower() for name in self.VANILLA_DLC_LIST}

            for dir_path in dlc_dirs:
                dlc_name = os.path.basename(dir_path).lower()
                if dlc_name not in vanilla_dlc_names:
                    continue
                for root, dirs, files in os.walk(dir_path):
                    for file in sorted(files):
                        if file.endswith('.cache') and Cache.GetCacheTypeOfFile(os.path.join(root, file)) == Cache.Cachetype.Texture:
                            self.LoadBundle(os.path.join(root, file))

        # folders_to_check = ['dlc', 'content']
        # for folder in folders_to_check:
        #     folder_path = os.path.join(self.base_path, folder)
        #     if os.path.exists(folder_path):
        #         for root, dirs, files in os.walk(folder_path):
        #             for file in files:
        #                 if file.endswith('texture.cache'):
        #                     cache_files.append(os.path.join(root, file))

        # self.cache_files = cache_files
    
    def OpenFile(self):
        pass

    def GetString(self):
        pass
    @classmethod
    def from_json(cls, data):
        pass
    @staticmethod
    def Get(do_reload=False, loadmods=False):
        current_base_path = _refresh_texture_configuration_path()
        instance_manager = TextureManager.InstanceManagerMods if loadmods else TextureManager.InstanceManager
        cache_name = "texture_cache_mods.pkl" if loadmods else "texture_cache.pkl"

        if (
            instance_manager is not None
            and getattr(instance_manager, "base_path", None) != current_base_path
        ):
            do_reload = True

        if instance_manager is None or do_reload:
            cache_root = get_cache_root(create=True)
            cache_dir = os.path.join(cache_root, "TextureCache")
            os.makedirs(cache_dir, exist_ok=True)
            filename = os.path.join(cache_dir, cache_name)
            meta_path = cache_meta.get_meta_path(filename)
            start_time = time.time()

            def load_tm(filename):
                tm = TextureManager()
                if loadmods:
                    tm.LoadModsBundles(Configuration.GameModDir, Configuration.GameDlcDir)
                else:
                    tm.LoadAll(current_base_path)
                try:
                    with open(filename, 'wb') as f:
                        pickle.dump(tm, f, protocol=pickle.HIGHEST_PROTOCOL)
                except Exception as e:
                    log.warning("Failed to save texture cache: %s", e)
                signature, source = TextureManager.BuildSourceSignature(loadmods)
                meta = cache_meta.make_meta(cache_name, filename, signature, source)
                cache_meta.save_meta(meta_path, meta)
                return tm

            if not os.path.exists(filename) or do_reload:
                tm = load_tm(filename)
            else:
                # Validate cache signature before loading from pickle
                meta = cache_meta.load_meta(meta_path)
                current_sig, _ = TextureManager.BuildSourceSignature(loadmods)
                if not cache_meta.signatures_match(meta.get("signature", {}), current_sig):
                    log.info('Texture cache stale, rebuilding %s...', "mods" if loadmods else "vanilla")
                    tm = load_tm(filename)
                else:
                    with open(filename, 'rb') as f:
                        try:
                            tm = pickle.load(f)
                        except Exception as e:
                            tm = load_tm(filename)
            time_taken = time.time() - start_time
            log.info('Loaded Texture Cache in %.2f seconds (%d items)', time_taken, len(tm.Items))
            if loadmods:
                TextureManager.InstanceManagerMods = tm
            else:
                TextureManager.InstanceManager = tm
            instance_manager = tm

        return instance_manager

    @staticmethod
    def BuildSourceSignature(loadmods=False):
        base_path = _refresh_texture_configuration_path()
        if loadmods:
            roots = cache_meta.get_mod_dirs(os.path.join(base_path, "mods"))
            dlc_dirs = cache_meta.get_dlc_dirs(base_path, vanilla_only=False, vanilla_list=TextureManager.VANILLA_DLC_LIST)
            vanilla_set = {v.lower() for v in TextureManager.VANILLA_DLC_LIST}
            mod_dlc_dirs = [d for d in dlc_dirs if os.path.basename(d).lower() not in vanilla_set]
            roots.extend(mod_dlc_dirs)
        else:
            roots = cache_meta.get_content_patch_dirs(base_path)
            roots.extend(
                cache_meta.get_dlc_dirs(
                    base_path,
                    vanilla_only=True,
                    vanilla_list=TextureManager.VANILLA_DLC_LIST,
                )
            )

        def _predicate(path: str) -> bool:
            if not path.lower().endswith(".cache"):
                return False
            return Cache.GetCacheTypeOfFile(path) == Cache.Cachetype.Texture

        signature = cache_meta.compute_signature(cache_meta.iter_files(roots, _predicate))
        source = {
            "type": "texture_cache_mods" if loadmods else "texture_cache",
            "base_path": base_path,
            "roots": roots,
        }
        return signature, source
