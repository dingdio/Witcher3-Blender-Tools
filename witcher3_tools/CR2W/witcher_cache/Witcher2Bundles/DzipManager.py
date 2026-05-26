from __future__ import annotations

from collections import defaultdict
import logging
import os
import pickle
import re
import time

from .DzipArchive import DzipArchive, InvalidDzipException
from .. import cache_meta
from ....extension_paths import get_cache_root

log = logging.getLogger(__name__)


def natural_sort_key(value: str):
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r"(\d+)", value)]


def _normalize_path(path: str) -> str:
    if not path:
        return ""
    try:
        return os.path.normpath(path)
    except Exception:
        return path


def _get_addon_prefs():
    try:
        import bpy
    except Exception:
        return None

    context = getattr(bpy, "context", None)
    prefs_root = getattr(context, "preferences", None) if context else None
    addons = getattr(prefs_root, "addons", None) if prefs_root else None
    if not addons:
        return None

    try:
        values = addons.values()
    except Exception:
        values = addons

    try:
        for addon in values:
            prefs = getattr(addon, "preferences", None)
            if prefs is not None and hasattr(prefs, "witcher2_game_path"):
                return prefs
    except Exception:
        return None
    return None


def _current_witcher2_game_path() -> str:
    prefs = _get_addon_prefs()
    if not prefs:
        return ""
    return _normalize_path(str(getattr(prefs, "witcher2_game_path", "") or "").strip())


def _has_witcher2_cooked_root(game_path: str) -> bool:
    return bool(game_path and os.path.isdir(os.path.join(game_path, "CookedPC")))


def _iter_dzip_files(game_path: str):
    cooked_root = os.path.join(game_path, "CookedPC")
    if not os.path.isdir(cooked_root):
        return []

    paths = []
    for root, _dirs, files in os.walk(cooked_root):
        for name in files:
            if name.lower().endswith(".dzip"):
                paths.append(os.path.join(root, name))

    def _sort_key(path):
        name = os.path.basename(path).lower()
        priority = 0 if name == "pack0.dzip" else 1
        return (priority, natural_sort_key(name), natural_sort_key(path))

    paths.sort(key=_sort_key)
    return paths


class DzipManager:
    InstanceManager = None

    def __init__(self):
        self.base_path = ""
        self.cache_files = []
        self.Items = defaultdict(list)
        self.Bundles = {}
        self.FileList = []
        self.Extensions = []
        self.AutocompleteSource = []

    @property
    def TypeName(self):
        return "Witcher2DZIP"

    @staticmethod
    def SerializationVersion():
        return "1.0"

    def find_item_by_hash(self, value):
        key = str(value or "").replace("/", "\\")
        return self.Items.get(key, None)

    def find_item_by_path_name(self, value):
        return self.find_item_by_hash(value)

    def LoadDzip(self, filename):
        if filename in self.Bundles:
            return

        archive = DzipArchive(filename)
        for key, item in archive.Items.items():
            self.Items[key].append(item)
            self.FileList.append(key)
            ext = os.path.splitext(key)[1].lower()
            if ext and ext not in self.Extensions:
                self.Extensions.append(ext)

        self.Bundles[filename] = archive

    def LoadAll(self, base_path):
        self.base_path = _normalize_path(base_path)
        self.cache_files = []
        self.Items = defaultdict(list)
        self.Bundles = {}
        self.FileList = []
        self.Extensions = []
        self.AutocompleteSource = []

        if not _has_witcher2_cooked_root(self.base_path):
            log.info("Witcher 2 DZIP cache skipped: game path not set or invalid: %s", self.base_path or "<unset>")
            return

        self.cache_files = _iter_dzip_files(self.base_path)
        for filename in self.cache_files:
            try:
                self.LoadDzip(filename)
            except InvalidDzipException as exc:
                log.warning("Skipping invalid Witcher 2 DZIP '%s': %s", filename, exc)
            except Exception:
                log.warning("Failed to load Witcher 2 DZIP '%s'", filename, exc_info=True)

        self.Extensions.sort()
        self.AutocompleteSource = sorted(self.Items.keys(), key=natural_sort_key)

    @staticmethod
    def BuildSourceSignature(base_path: str):
        files = _iter_dzip_files(base_path) if _has_witcher2_cooked_root(base_path) else []
        signature = cache_meta.compute_signature(files)
        source = {
            "type": "witcher2_dzip",
            "base_path": base_path,
            "roots": [os.path.join(base_path, "CookedPC")] if base_path else [],
            "files": files,
        }
        return signature, source

    @staticmethod
    def Get(reset_cache=False, game_path: str = ""):
        current_base_path = _normalize_path(game_path or _current_witcher2_game_path())
        instance_manager = DzipManager.InstanceManager

        if instance_manager is not None and getattr(instance_manager, "base_path", None) != current_base_path:
            reset_cache = True

        if not _has_witcher2_cooked_root(current_base_path):
            manager = DzipManager()
            manager.base_path = current_base_path
            DzipManager.InstanceManager = manager
            return manager

        if instance_manager is None or reset_cache:
            cache_root = get_cache_root(create=True)
            cache_dir = os.path.join(cache_root, "Witcher2Bundles")
            os.makedirs(cache_dir, exist_ok=True)
            cache_name = "w2_dzip_cache.pkl"
            cache_path = os.path.join(cache_dir, cache_name)

            start_time = time.time()

            def load_manager():
                manager = DzipManager()
                manager.LoadAll(current_base_path)
                with open(cache_path, "wb") as handle:
                    pickle.dump(manager, handle, protocol=pickle.HIGHEST_PROTOCOL)

                signature, source = DzipManager.BuildSourceSignature(current_base_path)
                meta_path = cache_meta.get_meta_path(cache_path)
                meta = cache_meta.make_meta(cache_name, cache_path, signature, source)
                cache_meta.save_meta(meta_path, meta)
                return manager

            if not os.path.exists(cache_path) or reset_cache:
                manager = load_manager()
            else:
                meta_path = cache_meta.get_meta_path(cache_path)
                meta = cache_meta.load_meta(meta_path)
                current_signature, _source = DzipManager.BuildSourceSignature(current_base_path)
                if not cache_meta.signatures_match(meta.get("signature", {}), current_signature):
                    log.info("Witcher 2 DZIP cache stale, rebuilding...")
                    manager = load_manager()
                else:
                    try:
                        with open(cache_path, "rb") as handle:
                            manager = pickle.load(handle)
                        if getattr(manager, "base_path", None) != current_base_path:
                            manager = load_manager()
                    except Exception:
                        manager = load_manager()

            log.info("Loaded Witcher 2 DZIP cache in %.2f seconds (%d items)", time.time() - start_time, len(manager.Items))
            DzipManager.InstanceManager = manager
            return manager

        return instance_manager
