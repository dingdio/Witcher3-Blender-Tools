import os
import time
import json
import pickle
from pathlib import Path
from ...bStream import *
from .W3StringFile import W3StringFile
from ..blender_common import get_game_path
from .. import cache_meta
from ....extension_paths import get_cache_root
import logging
log = logging.getLogger(__name__)


class Configuration:
    TextLanguage = "en"
    VoiceLanguage = "en"
    ExecutablePath = get_game_path()


def _normalize_game_path(path: str) -> str:
    if not path:
        return ""
    try:
        return os.path.normpath(os.path.abspath(path))
    except Exception:
        return os.path.normpath(path)


def _refresh_strings_configuration_path() -> str:
    current_path = _normalize_game_path(get_game_path())
    Configuration.ExecutablePath = current_path
    return current_path


def _has_string_source_root(base_path: str) -> bool:
    if not base_path:
        return False
    return os.path.isdir(os.path.join(base_path, "content")) or os.path.isdir(os.path.join(base_path, "dlc"))

class W3StringManager():
    InstanceManager = None
    def __init__(self):
        self.Language  = 'en'
        self.base_path = ""
        self.Lines:dict = {}
        self.Keys:dict = {}
        #self.importedStrings = {} #TODO
    def _looks_corrupted(self, sample_size: int = 200) -> bool:
        checked = 0
        bad = 0
        for s in self.Lines.values():
            checked += 1
            if not s:
                bad += 1
            else:
                printable = sum(1 for ch in s if ch.isprintable())
                if printable / max(len(s), 1) < 0.5:
                    bad += 1
            if checked >= sample_size:
                break
        if checked == 0:
            return True
        return (bad / checked) > 0.6
    
    def Load(self, newlanguage:str, path:str, onlyIfLanguageChanged:bool = False):
        if (onlyIfLanguageChanged and self.Language == newlanguage):
            return

        self.Language = newlanguage
        self.base_path = _normalize_game_path(path)
        self.Lines:dict = {}
        self.Keys:dict = {}

        if not _has_string_source_root(self.base_path):
            log.info("String cache skipped: Witcher 3 path not set or invalid: %s", self.base_path or "<unset>")
            return
        
        # gamedir = Path(path)
        # content = gamedir# / "content"
        # for file in list(content.rglob(self.Language+'.w3strings')):
        #     self.OpenFile(file)
        gamedir = Path(self.base_path)
        # Define specific subdirectories to search
        subdirs = ['content', 'dlc', 'mod', 'DLC', 'MOD']
        
        for subdir in subdirs:
            folder = gamedir / subdir
            if folder.is_dir():  # Check if the directory exists
                for file in folder.rglob(self.Language+'.w3strings'):
                    self.OpenFile(file)
        
    def OpenFile(self, filePath):
        try:
            # filePath = Path(r"<project>/content/en.w3strings")
            stringFile = W3StringFile()
            stream = bStream(path = filePath.absolute())
            stringFile.Read(stream)
        except Exception as e:
            raise e
        for item in stringFile.block1:
            self.Lines[item.str_id] = item.str
        for item in stringFile.block2:
            if item.str_id not in self.Keys:
                self.Keys[item.str_id] = True

    def GetString(self, id: int):
        return self.Lines.get(id)

    @classmethod
    def from_json(cls, data):
        t_class = cls()
        t_class.Language = data['Language']
        t_class.base_path = _normalize_game_path(data.get('base_path', ""))
        for key, val in data['Lines'].items():
            t_class.Lines[int(key)] = val
        for line in data['Keys'].items():
            t_class.Keys[int(line[0])] = line[1]
        return t_class
    @staticmethod
    def Get(do_reload = False):
        current_base_path = _refresh_strings_configuration_path()

        if (
            W3StringManager.InstanceManager is not None
            and getattr(W3StringManager.InstanceManager, "base_path", None) != current_base_path
        ):
            do_reload = True

        if (W3StringManager.InstanceManager == None or do_reload):
            cache_root = get_cache_root(create=True)
            cache_dir = os.path.join(cache_root, "W3Strings")
            os.makedirs(cache_dir, exist_ok=True)
            filename = os.path.join(cache_dir, "string_cache.pkl")
            meta_path = cache_meta.get_meta_path(filename)

            start_time = time.time()
            load_reason = "built (first time)"

            def build_from_game():
                w3StringManager = W3StringManager()
                w3StringManager.Load(Configuration.TextLanguage, current_base_path)

                # Blank-run friendly: do not overwrite cache/meta with an empty build from an invalid path.
                if not _has_string_source_root(current_base_path):
                    return w3StringManager

                with open(filename, "wb") as file:
                    pickle.dump(w3StringManager, file, protocol=pickle.HIGHEST_PROTOCOL)

                signature, source = cache_meta.signature_w3strings(current_base_path, Configuration.TextLanguage)
                meta = cache_meta.make_meta("string_cache.pkl", filename, signature, source)
                cache_meta.save_meta(meta_path, meta)
                return w3StringManager

            if not _has_string_source_root(current_base_path):
                w3StringManager = build_from_game()
            elif not os.path.exists(filename):
                w3StringManager = build_from_game()
            elif do_reload:
                load_reason = "rebuilt (forced)"
                w3StringManager = build_from_game()
            else:
                try:
                    with open(filename, "rb") as f:
                        w3StringManager = pickle.load(f)
                    if getattr(w3StringManager, "base_path", "") != current_base_path:
                        load_reason = "rebuilt (game path changed)"
                        w3StringManager = build_from_game()
                    elif w3StringManager._looks_corrupted():
                        load_reason = "rebuilt (corrupted cache)"
                        w3StringManager = build_from_game()
                    else:
                        load_reason = "loaded from cache"
                except Exception:
                    load_reason = "rebuilt (load error)"
                    w3StringManager = build_from_game()
            time_taken = time.time() - start_time
            log.info('Strings: %s in %.2fs', load_reason, time_taken)
            W3StringManager.InstanceManager = w3StringManager
        return W3StringManager.InstanceManager

    @staticmethod
    def BuildSourceSignature():
        base_path = _refresh_strings_configuration_path()
        return cache_meta.signature_w3strings(base_path, Configuration.TextLanguage)
