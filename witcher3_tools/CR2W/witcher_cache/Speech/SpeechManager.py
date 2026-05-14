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
from .... import dialog_language
import logging
log = logging.getLogger(__name__)

def natural_sort_key(s):
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s)]


def _speech_suffix(language):
    language = dialog_language.normalize_dialog_language(language)
    return f"{language.lower()}pc.w3speech"


def _has_speech_source_root(base_path, language):
    if not base_path or not os.path.isdir(base_path):
        return False
    if has_game_content_root(base_path):
        return True
    suffix = _speech_suffix(language)
    try:
        return any(
            os.path.isfile(os.path.join(base_path, filename))
            and filename.lower().endswith(suffix)
            for filename in os.listdir(base_path)
        )
    except OSError:
        return False

class SpeechManager(WitcherArchiveManager):
    InstanceManager = None
    InstanceManagers = {}
    def __init__(self):
        self.base_path = None
        self.language = "en"
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
        try:
            return self.Items.get(int(hash_value), None)
        except Exception:
            return None

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

    def LoadAll(self, base_path, language=None):
        self.base_path = normalize_game_path(base_path)
        self.language = dialog_language.normalize_dialog_language(language or dialog_language.get_active_voice_language())
        self.cache_files = []

        if not _has_speech_source_root(self.base_path, self.language):
            log.info("Speech cache skipped: Witcher 3 path not set or invalid: %s", self.base_path or "<unset>")
            return
        
        content = os.path.join(self.base_path, "content")
        dlc = os.path.join(self.base_path, "dlc")
        content_dirs = [d for d in os.listdir(content) if os.path.isdir(os.path.join(content, d)) and d.startswith("content")] if os.path.isdir(content) else []
        content_dirs.sort(key=natural_sort_key)
        patch_dirs = [d for d in os.listdir(content) if os.path.isdir(os.path.join(content, d)) and d.startswith("patch")] if os.path.isdir(content) else []
        patch_dirs.sort(key=natural_sort_key)
        speech_suffix = _speech_suffix(self.language)

        def scan_root(root_path):
            if not root_path or not os.path.isdir(root_path):
                return
            for root, dirs, files in os.walk(root_path):
                for file in sorted(files):
                    if file.lower().endswith(speech_suffix):
                        self.LoadBundle(os.path.join(root, file))

        # REDkit/source depots can keep a speech file directly in the selected root.
        for file in sorted(os.listdir(self.base_path)):
            full_path = os.path.join(self.base_path, file)
            if os.path.isfile(full_path) and file.lower().endswith(speech_suffix):
                self.LoadBundle(full_path)

        for dir_name in content_dirs + patch_dirs:
            scan_root(os.path.join(content, dir_name))

        if os.path.exists(dlc):
            dlc_dirs = [os.path.join(dlc, d) for d in os.listdir(dlc) if os.path.isdir(os.path.join(dlc, d))]
            dlc_dirs.sort(key=natural_sort_key)

            for dir_path in dlc_dirs:
                scan_root(dir_path)

        for mod_root_name in ("mods", "mod"):
            mod_root = os.path.join(self.base_path, mod_root_name)
            if not os.path.isdir(mod_root):
                continue
            mod_dirs = [os.path.join(mod_root, d) for d in os.listdir(mod_root) if os.path.isdir(os.path.join(mod_root, d))]
            mod_dirs.sort(key=natural_sort_key)
            for dir_path in mod_dirs:
                scan_root(dir_path)

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
    def Get(do_reload = False, language=None):
        current_base_path = refresh_game_configuration_path()
        current_language = dialog_language.normalize_dialog_language(language or dialog_language.get_active_voice_language())
        manager_key = (os.path.normcase(current_base_path or ""), current_language)
        instance = SpeechManager.InstanceManagers.get(manager_key)

        if (
            instance is not None
            and getattr(instance, "base_path", None) != current_base_path
        ):
            do_reload = True
        if (
            instance is not None
            and dialog_language.normalize_dialog_language(getattr(instance, "language", "en")) != current_language
        ):
            do_reload = True

        if (instance is None or do_reload):
            cache_root = get_cache_root(create=True)
            cache_dir = os.path.join(cache_root, "Speech")
            os.makedirs(cache_dir, exist_ok=True)
            filename = os.path.join(cache_dir, f"speech_cache_{current_language}.pkl")
            meta_path = cache_meta.get_meta_path(filename)
            
            start_time = time.time()
            
            def load_sm(filename):
                sm = SpeechManager()
                sm.LoadAll(current_base_path, language=current_language)

                # When no valid game path exists, return an empty manager without writing a misleading cache.
                if not _has_speech_source_root(current_base_path, current_language):
                    return sm

                with open(filename, 'wb') as f:
                    pickle.dump(sm, f, protocol=pickle.HIGHEST_PROTOCOL)

                signature, source = cache_meta.signature_w3speech(
                    current_base_path,
                    WitcherArchiveManager.VANILLA_DLC_LIST,
                    language=current_language,
                )
                meta = cache_meta.make_meta(f"speech_cache_{current_language}.pkl", filename, signature, source)
                cache_meta.save_meta(meta_path, meta)
                return sm
            
            if not _has_speech_source_root(current_base_path, current_language):
                sm = load_sm(filename)
            elif not os.path.exists(filename) or do_reload:
                sm = load_sm(filename)
            else:
                meta = cache_meta.load_meta(meta_path)
                current_sig, _ = SpeechManager.BuildSourceSignature(current_language)
                if not cache_meta.signatures_match(meta.get("signature", {}), current_sig):
                    log.info("Speech cache stale, rebuilding vanilla...")
                    sm = load_sm(filename)
                else:
                    try:
                        with open(filename, 'rb') as f:
                            sm = pickle.load(f)
                        if (
                            getattr(sm, "base_path", None) != current_base_path
                            or dialog_language.normalize_dialog_language(getattr(sm, "language", "en")) != current_language
                        ):
                            sm = load_sm(filename)
                    except Exception as e:
                        log.warning("Failed to load cached speech data, rebuilding: %s", e)
                        sm = load_sm(filename)
            time_taken = time.time() - start_time
            log.info('Loaded Speech Cache in %.2f seconds (%d files)', time_taken, len(sm.FileList))
            SpeechManager.InstanceManagers[manager_key] = sm
            SpeechManager.InstanceManager = sm
        else:
            SpeechManager.InstanceManager = instance
        return SpeechManager.InstanceManagers.get(manager_key, SpeechManager.InstanceManager)

    @staticmethod
    def BuildSourceSignature(language=None):
        base_path = refresh_game_configuration_path()
        return cache_meta.signature_w3speech(
            base_path,
            WitcherArchiveManager.VANILLA_DLC_LIST,
            language=language or dialog_language.get_active_voice_language(),
        )
