import logging
import os
import time
import re
from pathlib import Path
from typing import Dict, List, Optional
from collections import OrderedDict
import pickle
import gzip

log = logging.getLogger(__name__)

from ..common_cache.WitcherArchiveManager import (
    WitcherArchiveManager,
    Configuration,
    has_game_content_root,
    normalize_game_path,
    refresh_game_configuration_path,
)
from ..Cache import Cache
from .. import cache_meta
from ....extension_paths import get_cache_root
from .Collision_Cache import CollisionCache
from .CollisionCacheItem import CollisionCacheItem


def natural_sort_key(s):
    """Natural sorting key that handles embedded numbers correctly."""
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s)]


class CollisionManager(WitcherArchiveManager):
    """
    Manager for collision cache files (.cache).

    Provides singleton access to collision cache data with pickle caching
    for fast subsequent loads.
    """

    InstanceManager = None
    InstanceManagerMods = None
    CACHE_FILENAME = "collision_cache.pkl"
    CACHE_FILENAME_MODS = "collision_cache_mods.pkl"

    def __init__(self):
        self.base_path: Optional[str] = None
        self.cache_files: List[str] = []

        # Primary lookup: filepath -> list of CollisionCacheItem
        self.Items: Dict[str, List[CollisionCacheItem]] = OrderedDict()

        # Archive storage: cache filepath -> CollisionCache
        self.Archives: Dict[str, CollisionCache] = {}

        # Flat list of all items
        self.FileList: List[CollisionCacheItem] = []

        self.Extensions: List[str] = []
        self.AutocompleteSource: List[str] = []

    @property
    def TypeName(self):
        """Return the type name for this archive manager."""
        return "CollisionCache"

    def find_item_by_path_name(self, filepath: str) -> Optional[List[CollisionCacheItem]]:
        """
        Find collision cache items by file path.

        Args:
            filepath: The file path to search for (e.g., "levels\\novigrad\\novigrad.nxs")

        Returns:
            List of CollisionCacheItem or None if not found
        """
        return self.Items.get(filepath, None)

    def find_first_item_by_path_name(self, filepath: str) -> Optional[CollisionCacheItem]:
        """
        Find the first collision cache item matching the file path.

        Args:
            filepath: The file path to search for

        Returns:
            CollisionCacheItem or None if not found
        """
        items = self.Items.get(filepath, None)
        if items and len(items) > 0:
            return items[0]
        return None

    def LoadModBundle(self, filename: str):
        """
        Load a single mod collision cache.

        Mod files are prefixed with the mod folder name in the Items dict.

        Args:
            filename: Path to the .cache file
        """
        if filename in self.Archives:
            return

        try:
            bundle = CollisionCache(filename)
        except Exception as e:
            log.warning("Failed to load collision cache %s: %s", filename, e)
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
        """
        Load a single collision cache.

        Args:
            filename: Path to the .cache file
            ispatch: Whether this is a patch bundle (unused, for interface compatibility)
        """
        if filename in self.Archives:
            return

        try:
            bundle = CollisionCache(filename)
        except Exception as e:
            log.warning("Failed to load collision cache %s: %s", filename, e)
            return

        for item in bundle.Files:
            if item.Name not in self.Items:
                self.Items[item.Name] = []
            self.Items[item.Name].append(item)
            self.FileList.append(item)

        self.Archives[filename] = bundle
        self.cache_files.append(filename)

    def LoadAll(self, base_path: str):
        """
        Load all collision caches from the game directory.

        Scans:
        - content/content* directories
        - content/patch* directories
        - dlc/* directories (vanilla DLCs only)

        Args:
            base_path: Path to the game's executable directory
        """
        self.base_path = normalize_game_path(base_path)
        self.cache_files = []

        if not has_game_content_root(self.base_path):
            log.info("Collision cache skipped: Witcher 3 path not set or invalid: %s", self.base_path or "<unset>")
            return

        content = os.path.join(self.base_path, "content")
        dlc = os.path.join(self.base_path, "dlc")

        # Load content directories
        if os.path.exists(content):
            content_dirs = [
                d for d in os.listdir(content)
                if os.path.isdir(os.path.join(content, d)) and d.startswith("content")
            ]
            content_dirs.sort(key=natural_sort_key)

            patch_dirs = [
                d for d in os.listdir(content)
                if os.path.isdir(os.path.join(content, d)) and d.startswith("patch")
            ]
            patch_dirs.sort(key=natural_sort_key)

            for dir_name in content_dirs + patch_dirs:
                dir_path = os.path.join(content, dir_name)
                for root, dirs, files in os.walk(dir_path):
                    for file in files:
                        filepath = os.path.join(root, file)
                        if file.endswith('.cache') and Cache.GetCacheTypeOfFile(filepath) == Cache.Cachetype.Collision:
                            self.LoadBundle(filepath)

        # Load DLC directories (vanilla only)
        if os.path.exists(dlc):
            dlc_dirs = [
                os.path.join(dlc, d) for d in os.listdir(dlc)
                if os.path.isdir(os.path.join(dlc, d))
            ]
            dlc_dirs.sort(key=natural_sort_key)

            for dir_path in dlc_dirs:
                dlc_name = os.path.basename(dir_path).lower()
                if dlc_name in self.VANILLA_DLC_LIST:
                    for root, dirs, files in os.walk(dir_path):
                        for file in sorted(files):
                            filepath = os.path.join(root, file)
                            if file.endswith('.cache') and Cache.GetCacheTypeOfFile(filepath) == Cache.Cachetype.Collision:
                                self.LoadBundle(filepath)

    def LoadModsBundles(self, mods_path: str, dlc_path: str):
        """
        Load collision caches from mod directories.

        Args:
            mods_path: Path to the Mods directory
            dlc_path: Path to the DLC directory
        """
        self.base_path = normalize_game_path(Configuration.ExecutablePath)
        self.cache_files = []

        if not has_game_content_root(self.base_path):
            log.info("Collision cache skipped (mods): Witcher 3 path not set or invalid: %s", self.base_path or "<unset>")
            return
        if not mods_path:
            return
        if not os.path.exists(mods_path):
            os.makedirs(mods_path, exist_ok=True)

        mods_dirs = [
            os.path.join(mods_path, d) for d in os.listdir(mods_path)
            if os.path.isdir(os.path.join(mods_path, d))
        ]
        mods_dirs.sort(key=natural_sort_key)

        for dir_path in mods_dirs:
            for root, dirs, files in os.walk(dir_path):
                for file in files:
                    filepath = os.path.join(root, file)
                    if file.endswith('.cache') and Cache.GetCacheTypeOfFile(filepath) == Cache.Cachetype.Collision:
                        self.LoadModBundle(filepath)

        # Load non-vanilla DLCs (modded DLCs)
        if os.path.exists(dlc_path):
            dlc_dirs = [
                os.path.join(dlc_path, d) for d in os.listdir(dlc_path)
                if os.path.isdir(os.path.join(dlc_path, d))
            ]
            dlc_dirs.sort(key=natural_sort_key)

            for dir_path in dlc_dirs:
                dlc_name = os.path.basename(dir_path).lower()
                if dlc_name not in self.VANILLA_DLC_LIST:
                    for root, dirs, files in os.walk(dir_path):
                        for file in sorted(files):
                            filepath = os.path.join(root, file)
                            if file.endswith('.cache') and Cache.GetCacheTypeOfFile(filepath) == Cache.Cachetype.Collision:
                                self.LoadModBundle(filepath)

    def OpenFile(self):
        pass

    def GetString(self):
        pass

    @classmethod
    def from_json(cls, data):
        pass

    @staticmethod
    def Get(do_reload: bool = False, loadmods: bool = False) -> 'CollisionManager':
        """
        Get the singleton CollisionManager instance.

        Uses pickle caching to speed up subsequent loads.

        Args:
            do_reload: Force reload from game files instead of pickle cache
            loadmods: If True, load mod collision caches instead of vanilla

        Returns:
            CollisionManager instance
        """
        current_base_path = refresh_game_configuration_path()
        instance_manager = CollisionManager.InstanceManagerMods if loadmods else CollisionManager.InstanceManager
        cache_name = CollisionManager.CACHE_FILENAME_MODS if loadmods else CollisionManager.CACHE_FILENAME

        if (
            instance_manager is not None
            and getattr(instance_manager, "base_path", None) != current_base_path
        ):
            do_reload = True

        if not has_game_content_root(current_base_path):
            tm = CollisionManager()
            tm.base_path = current_base_path
            if loadmods:
                CollisionManager.InstanceManagerMods = tm
            else:
                CollisionManager.InstanceManager = tm
            return tm

        if instance_manager is None or do_reload:
            cache_root = get_cache_root(create=True)
            cache_dir = os.path.join(cache_root, "CollisionCache")
            os.makedirs(cache_dir, exist_ok=True)
            filename = os.path.join(cache_dir, cache_name)

            start_time = time.time()

            def load_from_game(cache_filename: str) -> CollisionManager:
                """Load from game files and save to pickle cache."""
                tm = CollisionManager()
                tm.base_path = current_base_path
                if loadmods:
                    tm.LoadModsBundles(Configuration.GameModDir, Configuration.GameDlcDir)
                else:
                    tm.LoadAll(current_base_path)

                if not has_game_content_root(current_base_path):
                    return tm

                # Save to pickle cache
                try:
                    with open(cache_filename, 'wb') as f:
                        pickle.dump(tm, f, protocol=pickle.HIGHEST_PROTOCOL)
                except Exception as e:
                    log.warning("Failed to save collision cache: %s", e)

                signature, source = CollisionManager.BuildSourceSignature(loadmods)
                meta_path = cache_meta.get_meta_path(cache_filename)
                meta = cache_meta.make_meta(cache_name, cache_filename, signature, source)
                cache_meta.save_meta(meta_path, meta)

                return tm

            if not os.path.exists(filename) or do_reload:
                tm = load_from_game(filename)
            else:
                # Validate cache signature before loading from pickle
                meta_path = cache_meta.get_meta_path(filename)
                meta = cache_meta.load_meta(meta_path)
                current_sig, _ = CollisionManager.BuildSourceSignature(loadmods)
                if not cache_meta.signatures_match(meta.get("signature", {}), current_sig):
                    log.info("Collision cache stale, rebuilding %s...", "mods" if loadmods else "vanilla")
                    tm = load_from_game(filename)
                else:
                    try:
                        with open(filename, 'rb') as f:
                            tm = pickle.load(f)
                        if getattr(tm, "base_path", None) != current_base_path:
                            tm = load_from_game(filename)
                    except Exception as e:
                        log.warning("Failed to load cached collision data, rebuilding: %s", e)
                        tm = load_from_game(filename)

            time_taken = time.time() - start_time
            log.info("Loaded Collision Cache in %.2f seconds (%d files)", time_taken, len(tm.FileList))
            if loadmods:
                CollisionManager.InstanceManagerMods = tm
            else:
                CollisionManager.InstanceManager = tm
            instance_manager = tm

        return instance_manager

    @staticmethod
    def BuildSourceSignature(loadmods: bool = False):
        base_path = refresh_game_configuration_path()
        if loadmods:
            roots = cache_meta.get_mod_dirs(os.path.join(base_path, "mods"))
            dlc_dirs = cache_meta.get_dlc_dirs(base_path, vanilla_only=False, vanilla_list=WitcherArchiveManager.VANILLA_DLC_LIST)
            vanilla_set = {v.lower() for v in WitcherArchiveManager.VANILLA_DLC_LIST}
            mod_dlc_dirs = [d for d in dlc_dirs if os.path.basename(d).lower() not in vanilla_set]
            roots.extend(mod_dlc_dirs)
        else:
            roots = cache_meta.get_content_patch_dirs(base_path)
            roots.extend(cache_meta.get_dlc_dirs(base_path, vanilla_only=True, vanilla_list=WitcherArchiveManager.VANILLA_DLC_LIST))

        def _predicate(path: str) -> bool:
            if not path.lower().endswith(".cache"):
                return False
            return Cache.GetCacheTypeOfFile(path) == Cache.Cachetype.Collision

        signature = cache_meta.compute_signature(cache_meta.iter_files(roots, _predicate))
        source = {
            "type": "collision_cache_mods" if loadmods else "collision_cache",
            "base_path": base_path,
            "roots": roots,
        }
        return signature, source

    @staticmethod
    def ResetInstance():
        """Reset the singleton instances (useful for testing or forced reload)."""
        CollisionManager.InstanceManager = None
        CollisionManager.InstanceManagerMods = None

    def __repr__(self):
        return f"CollisionManager({len(self.Archives)} archives, {len(self.FileList)} files)"
