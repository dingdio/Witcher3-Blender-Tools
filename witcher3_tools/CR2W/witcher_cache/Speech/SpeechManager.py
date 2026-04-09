import os
import time
import json
import re
from pathlib import Path
from ..common_cache.WitcherArchiveManager import (
    WitcherArchiveManager,
    EBundleType,
    Configuration,
    has_game_content_root,
    normalize_game_path,
    refresh_game_configuration_path,
)
# from .Cache import Cache
from .W3Speech import W3Speech
# from .SpeechCache import SpeechCache
import pickle
import gzip
from .. import cache_meta
from ....extension_paths import get_cache_root
import logging
log = logging.getLogger(__name__)

def natural_sort_key(s):
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s)]

class SpeechManager(WitcherArchiveManager):
    InstanceManager = None
    def __init__(self):
        self.base_path = None
        self.cache_files = None
        
        self.Items = {}  # Dictionary for string to list of IWitcherFile
        self.Speeches = {}  # Dictionary for string to SpeechCache
        self.FileList = []  # List of IWitcherFile objects
        self.HashDict = {}

        self.Extensions = []  # List of strings
        self.AutocompleteSource = []  # This can be a list in Python

        
        # Items = new Dictionary<string, List<IWitcherFile>>();
        # Speeches = new Dictionary<string, SpeechCache>();
        # FileList = new List<IWitcherFile>();

        # Extensions = new List<string>();
        # AutocompleteSource = new AutoCompleteStringCollection();

    # def find_item_by_hash(self, hash_value):
    #     for key in self.Items:
    #         for item in self.Items[key]:
    #             if item.Hash == hash_value:
    #                 return item
    #     return None
    

    @property
    def TypeName(self):
        return EBundleType.SPEECH
    
    def find_item_by_hash(self, hash_value):
        return self.Items.get(int(hash_value), None)

    def LoadBundle(self, filename):
        log.debug("Loading speech bundle: %s", filename)
        if filename in self.Speeches:
            return
        try:
            speech = W3Speech(filename)  # Assuming W3Speech is defined elsewhere
        except Exception as exc:
            log.warning("Failed to load speech bundle %s: %s", filename, exc)
            return
        self.cache_files.append(filename)
        for item in speech.item_infos:
            if item.name not in self.Items:
                self.Items[item.name] = []

            self.Items[item.name].append(item)
            self.FileList.append(item)

        self.Speeches[filename] = speech

    def LoadAll(self, base_path):
        self.base_path = normalize_game_path(base_path)
        self.cache_files = []

        if not has_game_content_root(self.base_path):
            log.info("Speech cache skipped: Witcher 3 path not set or invalid: %s", self.base_path or "<unset>")
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
                    if file.endswith('enpc.w3speech'):
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
                        if file.endswith('enpc.w3speech'):
                            self.LoadBundle(os.path.join(root, file))

    def OpenFile(self):
        pass

    def GetString(self):
        pass
    
    def LoadModBundle(arg):
        pass
    def LoadModsBundles(arg):
        pass
    
    @classmethod
    def from_json(cls, data):
        pass
    @staticmethod
    def Get(do_reload = False):
        current_base_path = refresh_game_configuration_path()

        if (
            SpeechManager.InstanceManager is not None
            and getattr(SpeechManager.InstanceManager, "base_path", None) != current_base_path
        ):
            do_reload = True

        if (SpeechManager.InstanceManager == None or do_reload):
            cache_root = get_cache_root(create=True)
            cache_dir = os.path.join(cache_root, "Speech")
            os.makedirs(cache_dir, exist_ok=True)
            filename = os.path.join(cache_dir, "speech_cache.pkl")
            meta_path = cache_meta.get_meta_path(filename)
            
            start_time = time.time()
            
            def load_sm(filename):
                sm = SpeechManager()
                sm.LoadAll(current_base_path)

                # When no valid game path exists, return an empty manager without writing a misleading cache.
                if not has_game_content_root(current_base_path):
                    return sm

                with open(filename, 'wb') as f:
                    pickle.dump(sm, f, protocol=pickle.HIGHEST_PROTOCOL)

                signature, source = cache_meta.signature_w3speech(current_base_path, WitcherArchiveManager.VANILLA_DLC_LIST)
                meta = cache_meta.make_meta("speech_cache.pkl", filename, signature, source)
                cache_meta.save_meta(meta_path, meta)
                return sm
            
            if not has_game_content_root(current_base_path):
                sm = load_sm(filename)
            elif not os.path.exists(filename) or do_reload:
                sm = load_sm(filename)
            else:
                meta = cache_meta.load_meta(meta_path)
                current_sig, _ = SpeechManager.BuildSourceSignature()
                if not cache_meta.signatures_match(meta.get("signature", {}), current_sig):
                    log.info("Speech cache stale, rebuilding vanilla...")
                    sm = load_sm(filename)
                else:
                    try:
                        with open(filename, 'rb') as f:
                            sm = pickle.load(f)
                        if getattr(sm, "base_path", None) != current_base_path:
                            sm = load_sm(filename)
                    except Exception as e:
                        log.warning("Failed to load cached speech data, rebuilding: %s", e)
                        sm = load_sm(filename)
            time_taken = time.time() - start_time
            log.info('Loaded Speech Cache in %.2f seconds (%d files)', time_taken, len(sm.FileList))
            SpeechManager.InstanceManager = sm
        return SpeechManager.InstanceManager

    @staticmethod
    def BuildSourceSignature():
        base_path = refresh_game_configuration_path()
        return cache_meta.signature_w3speech(base_path, WitcherArchiveManager.VANILLA_DLC_LIST)
