import os
import glob
from collections import defaultdict
from .Bundle import Bundle
import time
import re
import pickle
import gzip
from ..common_cache.WitcherArchiveManager import (
    WitcherArchiveManager,
    EBundleType,
    Configuration,
    has_game_content_root,
    normalize_game_path,
    refresh_game_configuration_path,
)
from .. import cache_meta
from ....extension_paths import get_cache_root
import logging
log = logging.getLogger(__name__)

def natural_sort_key(s):
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s)]

class BundleManager(WitcherArchiveManager):
    InstanceManager = None
    InstanceManagerMods = None
 
    def __init__(self):
        self.base_path = ""
        self.cache_files = []
        self.Items = defaultdict(list)
        self.Bundles = {}
        self.FileList = []
        self.Extensions = []
        self.AutocompleteSource = []
        self.locked_bundle_files = []
        self.failed_bundle_files = []
        self.cache_fallback_due_locked = False

    @property
    def TypeName(self):
        return EBundleType.BUNDLE

    @staticmethod
    def SerializationVersion():
        return "1.0"

    def find_item_by_hash(self, hash_value):
        return self.Items.get(hash_value, None)

    def find_item_by_partial_hash(self, start="items", end="t_01_mg__body.w2ent"):
        """Find bundle items by a start prefix and an end/basename match.

        Returns a flat list of BundleItem objects (empty list if no matches).
        Prefers exact basename matches when `end` has no path separator.
        """
        if not end:
            return []

        def _extend(target, items):
            if items is None:
                return
            if isinstance(items, list):
                target.extend(items)
            else:
                target.append(items)

        end_has_sep = ("\\" in end) or ("/" in end)
        exact_matches = []
        suffix_matches = []

        for key, items in self.Items.items():
            if not key.startswith(start):
                continue

            if end_has_sep:
                if key.endswith(end):
                    _extend(suffix_matches, items)
                continue

            # Prefer exact basename match when end is a filename (no separator)
            if os.path.basename(key) == end:
                _extend(exact_matches, items)
            elif key.endswith(end):
                _extend(suffix_matches, items)

        if exact_matches:
            return exact_matches
        return suffix_matches

    def LoadModBundle(self, filename):
        if filename in self.Bundles:
            return

        try:
            bundle = Bundle(filename)
        except PermissionError as exc:
            self.locked_bundle_files.append(filename)
            log.warning("Skipped locked mod bundle %s: %s", filename, exc)
            return
        except Exception as exc:
            self.failed_bundle_files.append(filename)
            log.warning("Failed to load mod bundle %s: %s", filename, exc)
            return

        for key, value in bundle.Items.items():
            mod_folder = self.GetModFolder(filename) + "\\" + key
            if mod_folder not in self.Items:
                self.Items[mod_folder] = []

            self.Items[mod_folder].append(value)

        self.Bundles[filename] = bundle

    def LoadBundle(self, filename, ispatch=False):
        if filename in self.Bundles:
            return

        try:
            bundle = Bundle(filename)  # Assuming Bundle is a class you've defined
        except PermissionError as exc:
            self.locked_bundle_files.append(filename)
            log.warning("Skipped locked bundle %s: %s", filename, exc)
            return
        except Exception as exc:
            self.failed_bundle_files.append(filename)
            log.warning("Failed to load bundle %s: %s", filename, exc)
            return

        for key, value in bundle.Items.items():
            if key not in self.Items:
                self.Items[key] = []

            if ispatch and len(self.Items[key]) > 0:
                files_in_bundles = self.Items[key]
                splits = files_in_bundles[0].Bundle.ArchiveAbsolutePath.split(os.sep)
                contentdir = splits[-3]
                if "content" in contentdir:
                    while len(files_in_bundles) > 0:
                        bundle.Patchedfiles.append(files_in_bundles[0])
                        files_in_bundles.pop(0)

            self.Items[key].append(value)

        self.Bundles[filename] = bundle

    def LoadAll(self, base_path):
        self.base_path = normalize_game_path(base_path)
        self.cache_files = []

        if not has_game_content_root(self.base_path):
            log.info("Bundle cache skipped (vanilla): Witcher 3 path not set or invalid: %s", self.base_path or "<unset>")
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
                    if file.endswith('.bundle'):
                        self.LoadBundle(os.path.join(root, file))

        if os.path.exists(dlc):
            dlc_dirs = [os.path.join(dlc, d) for d in os.listdir(dlc) if os.path.isdir(os.path.join(dlc, d))]
            dlc_dirs.sort(key=natural_sort_key)

            for dir_path in dlc_dirs:
                if os.path.basename(dir_path).lower() in self.VANILLA_DLC_LIST:
                    for root, dirs, files in os.walk(dir_path):
                        for file in sorted(files):
                            if file.endswith('.bundle'):
                                self.LoadBundle(os.path.join(root, file))

    def LoadModsBundles(self, mods, dlc):
        # Mods cache depends on a valid game root for mods/dlc locations.
        self.base_path = normalize_game_path(Configuration.ExecutablePath)
        self.cache_files = []

        mod_roots = cache_meta.get_mod_roots(self.base_path)
        if mods and os.path.isdir(mods):
            mods_norm = os.path.normpath(mods)
            if os.path.normcase(os.path.abspath(mods_norm)) not in {
                os.path.normcase(os.path.abspath(path)) for path in mod_roots
            }:
                mod_roots.append(mods_norm)

        if not (
            has_game_content_root(self.base_path)
            or mod_roots
            or (dlc and os.path.isdir(dlc))
        ):
            log.info("Bundle cache skipped (mods): Witcher 3 path not set or invalid: %s", self.base_path or "<unset>")
            return

        for mods_root in mod_roots:
            modsdirs = cache_meta.get_mod_dirs(mods_root)
            modbundles = [
                file
                for dir_path in modsdirs
                for file in glob.glob(os.path.join(dir_path, '**', '*.bundle'), recursive=True)
            ]
            for file in modbundles:
                self.LoadModBundle(file)

        if dlc and os.path.exists(dlc):
            dlcdirs = sorted(glob.glob(os.path.join(dlc, '*')))
            dlcfiles = [file
                        for dir in dlcdirs
                        if os.path.basename(dir).lower() not in self.VANILLA_DLC_LIST
                        for file in glob.glob(os.path.join(dir, '**', '*.bundle'), recursive=True)
                        ]
            
            for file in dlcfiles:
                self.LoadModBundle(file)

        #self.RebuildRootNode()
    
    @staticmethod
    def Get(loadmods = True, reset_cache = False):
        current_base_path = refresh_game_configuration_path()
        instance_manager = BundleManager.InstanceManagerMods if loadmods else BundleManager.InstanceManager
        cache_name = "bundle_cache_mods.pkl" if loadmods else "bundle_cache.pkl"

        if (
            instance_manager is not None
            and getattr(instance_manager, "base_path", None) != current_base_path
        ):
            reset_cache = True

        has_source_root = has_game_content_root(current_base_path)
        if loadmods:
            has_source_root = has_source_root or bool(
                cache_meta.get_mod_roots(current_base_path)
                or os.path.isdir(os.path.join(current_base_path, "dlc"))
            )

        if not has_source_root:
            bm = BundleManager()
            bm.base_path = current_base_path
            if loadmods:
                BundleManager.InstanceManagerMods = bm
            else:
                BundleManager.InstanceManager = bm
            return bm
        
        if (instance_manager == None or reset_cache):
            cache_root = get_cache_root(create=True)
            cache_dir = os.path.join(cache_root, "Bundles")
            os.makedirs(cache_dir, exist_ok=True)
            filename = os.path.join(cache_dir, cache_name)
            
            start_time = time.time()

            def load_cached_bm_if_valid():
                if not os.path.exists(filename):
                    return None
                meta_path = cache_meta.get_meta_path(filename)
                meta = cache_meta.load_meta(meta_path)
                current_sig, _ = BundleManager.BuildSourceSignature(loadmods)
                if not cache_meta.signatures_match(meta.get("signature", {}), current_sig):
                    return None
                try:
                    with open(filename, 'rb') as f:
                        cached_bm = pickle.load(f)
                except Exception:
                    return None
                if getattr(cached_bm, "base_path", None) != current_base_path:
                    return None
                return cached_bm
            
            def load_bm(filename):
                bm = BundleManager()
                bm.base_path = current_base_path
                if loadmods:
                    bm.LoadModsBundles(Configuration.GameModDir, Configuration.GameDlcDir)
                else:
                    bm.LoadAll(current_base_path)

                if bm.locked_bundle_files:
                    cached_bm = load_cached_bm_if_valid()
                    if cached_bm is not None:
                        cached_bm.locked_bundle_files = list(bm.locked_bundle_files)
                        cached_bm.failed_bundle_files = list(getattr(bm, "failed_bundle_files", []) or [])
                        cached_bm.cache_fallback_due_locked = True
                        return cached_bm
                    log.warning(
                        "Bundle cache incomplete; %d bundle file(s) are locked.",
                        len(bm.locked_bundle_files),
                    )
                else:
                    with open(filename, 'wb') as f:
                        pickle.dump(bm, f, protocol=pickle.HIGHEST_PROTOCOL)

                    signature, source = BundleManager.BuildSourceSignature(loadmods)
                    meta_path = cache_meta.get_meta_path(filename)
                    meta = cache_meta.make_meta(cache_name, filename, signature, source)
                    cache_meta.save_meta(meta_path, meta)
                return bm
            
            if not os.path.exists(filename) or reset_cache:
                bm = load_bm(filename)
            else:
                # Validate cache signature before loading from pickle
                bm = load_cached_bm_if_valid()
                if bm is None:
                    log.info('Bundle cache stale, rebuilding %s...', "mods" if loadmods else "vanilla")
                    bm = load_bm(filename)
            time_taken = time.time() - start_time
            log.info('Loaded Bundle Cache in %.2f seconds (%d items)', time_taken, len(bm.Items))
            instance_manager = bm
            if loadmods:
                BundleManager.InstanceManagerMods = bm
            else:
                BundleManager.InstanceManager = bm
        return instance_manager

    @staticmethod
    def BuildSourceSignature(loadmods: bool):
        base_path = refresh_game_configuration_path()
        if loadmods:
            return cache_meta.signature_bundles_mods(base_path, WitcherArchiveManager.VANILLA_DLC_LIST)
        return cache_meta.signature_bundles_base(base_path, WitcherArchiveManager.VANILLA_DLC_LIST)

def RebuildRootNode(self):
    # Placeholder for implementation
    pass
