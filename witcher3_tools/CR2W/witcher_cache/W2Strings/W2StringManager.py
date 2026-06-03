from __future__ import annotations

import logging
import os
import pickle
import time
from io import BytesIO

from .. import cache_meta
from ....extension_paths import get_cache_root
from .W2Language import normalize_language_handle, language_handle_from_filename
from .W2StringFile import W2StringFile, W2StringsParseError, find_w2_strings_files

log = logging.getLogger(__name__)


class Configuration:
    TextLanguage = "en"
    ExecutablePath = ""


_STRING_CACHE_FORMAT_VERSION = 2


def _normalize_path(path: str) -> str:
    if not path:
        return ""
    try:
        return os.path.normpath(os.path.abspath(path))
    except Exception:
        return os.path.normpath(path)


def _coerce_roots(roots):
    if roots is None:
        return []
    if isinstance(roots, (str, os.PathLike)):
        return [roots]
    try:
        iterator = iter(roots)
    except TypeError:
        return [roots]
    return [root for root in iterator if root]


def _roots_cache_key(roots) -> str:
    parts = []
    seen = set()
    for root in _coerce_roots(roots):
        norm = _normalize_path(str(root))
        if not norm:
            continue
        key = os.path.normcase(norm)
        if key in seen:
            continue
        seen.add(key)
        parts.append(norm)
    return os.pathsep.join(parts)


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


def _redkit_project_paths(prefs):
    paths = []
    try:
        for item in getattr(prefs, "redkit_projects", []) or []:
            path = str(getattr(item, "path", "") or "").strip()
            if path:
                paths.append(path)
    except Exception:
        pass
    return paths


def current_witcher2_string_roots():
    prefs = _get_addon_prefs()
    if prefs is None:
        return []
    roots = []
    for attr in ("witcher2_game_path", "w2_unbundle_path"):
        value = str(getattr(prefs, attr, "") or "").strip()
        if value:
            roots.append(value)
    roots.extend(_redkit_project_paths(prefs))
    return roots


def resolve_w2_strings_roots(override_path: str = "", fallback_roots=None):
    override_path = str(override_path or "").strip()
    if override_path:
        return [override_path]
    if fallback_roots is not None:
        return _coerce_roots(fallback_roots)
    return current_witcher2_string_roots()


def find_w2_strings_files_for_path(override_path: str = "", fallback_roots=None):
    return find_w2_strings_files(resolve_w2_strings_roots(override_path, fallback_roots))


def _find_w2_dzip_files(roots):
    found = []
    seen = set()
    for root in _coerce_roots(roots):
        root_str = str(root or "").strip()
        if not root_str:
            continue
        cooked_root = os.path.join(root_str, "CookedPC")
        if not os.path.isdir(cooked_root):
            continue
        for dirpath, _dirs, filenames in os.walk(cooked_root):
            for name in sorted(filenames):
                if not name.lower().endswith(".dzip"):
                    continue
                path = os.path.join(dirpath, name)
                key = os.path.normcase(os.path.normpath(path))
                if key in seen:
                    continue
                seen.add(key)
                found.append(path)
    return found


def _dzip_item_source_id(item):
    archive_path = str(getattr(getattr(item, "bundle", None), "ArchiveAbsolutePath", "") or "")
    item_name = str(getattr(item, "name", "") or getattr(item, "Name", "") or "")
    return f"{archive_path}::{item_name}" if archive_path else item_name


def _iter_w2_strings_dzip_sources(roots):
    seen = set()
    for root in _coerce_roots(roots):
        root_str = str(root or "").strip()
        if not root_str or not os.path.isdir(os.path.join(root_str, "CookedPC")):
            continue
        try:
            from ..Witcher2Bundles import LoadWitcher2BundleManager

            manager = LoadWitcher2BundleManager(game_path=root_str)
        except Exception:
            log.debug("Could not load Witcher 2 DZIP manager for strings root %s.", root_str, exc_info=True)
            continue

        for item_name in sorted(getattr(manager, "Items", {}) or {}):
            if not str(item_name or "").lower().endswith(".w2strings"):
                continue
            items = getattr(manager, "Items", {}).get(item_name) or []
            for item in items[:1]:
                source_id = _dzip_item_source_id(item)
                key = os.path.normcase(source_id)
                if not source_id or key in seen:
                    continue
                seen.add(key)
                yield source_id, item


def _find_w2_strings_sources(roots):
    sources = list(find_w2_strings_files(roots))
    sources.extend(_iter_w2_strings_dzip_sources(roots))
    return sources


def _w2_strings_source_label(source):
    if isinstance(source, tuple):
        return str(source[0] or "")
    return str(source or "")


def _w2_strings_source_language_path(source):
    if isinstance(source, tuple):
        item = source[1]
        return str(getattr(item, "name", "") or getattr(item, "Name", "") or source[0] or "")
    return str(source or "")


def _select_language_files(candidates, language: str):
    handle = normalize_language_handle(language)

    def _for_language(target_handle):
        return [p for p in candidates if language_handle_from_filename(_w2_strings_source_language_path(p)) == target_handle]

    matched = _for_language(handle)
    used_handle = handle
    if not matched and handle != "en":
        log.info("No %s.w2strings found; falling back to English.", handle.upper())
        matched = _for_language("en")
        used_handle = "en"
    if not matched and candidates:
        matched = candidates[:1]
        used_handle = language_handle_from_filename(_w2_strings_source_language_path(matched[0]))
    return matched, used_handle


class W2StringManager:
    InstanceManager = None
    InstanceManagers = {}

    def __init__(self):
        self.RequestedLanguage = "en"
        self.Language = "en"
        self.base_path = ""
        self.source_roots = []
        self.cache_files = []
        self.Lines = {}
        self.Keys = {}
        self.KeyToId = {}
        self.IdToKey = {}
        self.SourcePathById = {}
        self.string_cache_format_version = _STRING_CACHE_FORMAT_VERSION

    def _looks_corrupted(self, sample_size: int = 200) -> bool:
        checked = 0
        bad = 0
        for text in self.Lines.values():
            checked += 1
            if not text:
                bad += 1
            else:
                printable = sum(1 for ch in text if ch.isprintable())
                if printable / max(len(text), 1) < 0.5:
                    bad += 1
            if checked >= sample_size:
                break
        if checked == 0:
            return True
        return (bad / checked) > 0.6

    def Load(self, newlanguage: str, roots=None, onlyIfLanguageChanged: bool = False):
        language = normalize_language_handle(newlanguage)
        source_roots = resolve_w2_strings_roots(fallback_roots=roots)
        base_key = _roots_cache_key(source_roots)

        if onlyIfLanguageChanged and self.RequestedLanguage == language and self.base_path == base_key:
            return

        self.RequestedLanguage = language
        self.Language = language
        self.base_path = base_key
        self.source_roots = [_normalize_path(str(root)) for root in source_roots if str(root or "").strip()]
        self.cache_files = []
        self.Lines = {}
        self.Keys = {}
        self.KeyToId = {}
        self.IdToKey = {}
        self.SourcePathById = {}
        self.string_cache_format_version = _STRING_CACHE_FORMAT_VERSION

        candidates = _find_w2_strings_sources(source_roots)
        if not candidates:
            log.info("Witcher 2 strings cache skipped: no loose or archived .w2strings files found in %s", self.base_path or "<unset>")
            return

        matched, used_language = _select_language_files(candidates, language)
        self.Language = used_language
        self.cache_files = [_w2_strings_source_label(source) for source in matched]

        for source in matched:
            if isinstance(source, tuple):
                self.OpenDzipItem(source[0], source[1])
            else:
                self.OpenFile(source)

    def _ingest_string_file(self, string_file, source_label):
        for sid, text in string_file.texts.items():
            try:
                sid_int = int(sid)
            except (TypeError, ValueError):
                continue
            if sid_int not in self.Lines:
                self.Lines[sid_int] = text
                self.SourcePathById[sid_int] = str(source_label)

        for key_name, sid in string_file.keys.items():
            try:
                sid_int = int(sid)
            except (TypeError, ValueError):
                continue
            key_text = str(key_name or "").strip()
            if not key_text:
                continue
            self.Keys[sid_int] = True
            self.KeyToId[key_text] = sid_int
            self.KeyToId.setdefault(key_text.lower(), sid_int)
            self.IdToKey.setdefault(sid_int, key_text)
        return True

    def OpenFile(self, filePath):
        try:
            string_file = W2StringFile.from_file(filePath)
        except (W2StringsParseError, OSError, ValueError):
            log.warning("Could not read W2 strings file %s", filePath, exc_info=True)
            return False
        return self._ingest_string_file(string_file, filePath)

    def OpenDzipItem(self, source_label, item):
        try:
            buffer = BytesIO()
            item.extract(buffer)
            source_path = str(getattr(item, "name", "") or getattr(item, "Name", "") or source_label or "")
            string_file = W2StringFile.from_bytes(buffer.getvalue(), source_path=source_path)
        except Exception:
            log.warning("Could not read W2 strings archive item %s", source_label, exc_info=True)
            return False
        return self._ingest_string_file(string_file, source_label)

    def GetString(self, id: int):
        try:
            return self.Lines.get(int(id))
        except Exception:
            return None

    def GetStringByKey(self, key: str):
        key = str(key or "").strip()
        if not key:
            return None
        numeric_key = key[1:] if key.startswith("#") else key
        try:
            localized = self.GetString(int(numeric_key, 0))
            if localized:
                return localized
        except Exception:
            pass
        string_id = self.KeyToId.get(key)
        if string_id is None:
            string_id = self.KeyToId.get(key.lower())
        if string_id is None:
            return None
        return self.GetString(string_id)

    @staticmethod
    def BuildSourceSignature(base_path: str = "", language: str = None, roots=None):
        requested_language = normalize_language_handle(language or Configuration.TextLanguage)
        source_roots = resolve_w2_strings_roots(base_path, roots)
        files = find_w2_strings_files(source_roots)
        dzip_files = _find_w2_dzip_files(source_roots)
        signature_files = files + dzip_files
        signature = cache_meta.compute_signature(signature_files)
        source = {
            "type": "w2strings",
            "language": requested_language,
            "base_path": _roots_cache_key(source_roots),
            "roots": [_normalize_path(str(root)) for root in source_roots if str(root or "").strip()],
            "files": files,
            "dzip_files": dzip_files,
        }
        return signature, source

    @staticmethod
    def Get(do_reload=False, language=None, game_path: str = "", roots=None):
        requested_language = normalize_language_handle(language or Configuration.TextLanguage)
        source_roots = resolve_w2_strings_roots(game_path, roots)
        current_base_path = _roots_cache_key(source_roots)
        Configuration.TextLanguage = requested_language
        Configuration.ExecutablePath = current_base_path

        manager_key = (os.path.normcase(current_base_path or ""), requested_language)
        instance = W2StringManager.InstanceManagers.get(manager_key)

        if instance is not None:
            if getattr(instance, "base_path", None) != current_base_path:
                do_reload = True
            if getattr(instance, "RequestedLanguage", "") != requested_language:
                do_reload = True
            if getattr(instance, "string_cache_format_version", 1) != _STRING_CACHE_FORMAT_VERSION:
                do_reload = True

        if instance is None or do_reload:
            cache_root = get_cache_root(create=True)
            cache_dir = os.path.join(cache_root, "W2Strings")
            os.makedirs(cache_dir, exist_ok=True)
            cache_name = f"w2_string_cache_{requested_language}.pkl"
            cache_path = os.path.join(cache_dir, cache_name)
            meta_path = cache_meta.get_meta_path(cache_path)

            start_time = time.time()
            load_reason = "built"

            def build_manager():
                manager = W2StringManager()
                manager.Load(requested_language, source_roots)
                if not manager.cache_files:
                    return manager

                with open(cache_path, "wb") as handle:
                    pickle.dump(manager, handle, protocol=pickle.HIGHEST_PROTOCOL)

                signature, source = W2StringManager.BuildSourceSignature(
                    language=requested_language,
                    roots=source_roots,
                )
                meta = cache_meta.make_meta(cache_name, cache_path, signature, source)
                cache_meta.save_meta(meta_path, meta)
                return manager

            if not find_w2_strings_files(source_roots) and not _find_w2_dzip_files(source_roots):
                manager = build_manager()
            elif not os.path.exists(cache_path) or do_reload:
                if do_reload:
                    load_reason = "rebuilt (forced)"
                manager = build_manager()
            else:
                meta = cache_meta.load_meta(meta_path)
                current_signature, _source = W2StringManager.BuildSourceSignature(
                    language=requested_language,
                    roots=source_roots,
                )
                if not cache_meta.signatures_match(meta.get("signature", {}), current_signature):
                    load_reason = "rebuilt (sources changed)"
                    manager = build_manager()
                else:
                    try:
                        with open(cache_path, "rb") as handle:
                            manager = pickle.load(handle)
                        if (
                            getattr(manager, "base_path", None) != current_base_path
                            or getattr(manager, "RequestedLanguage", "") != requested_language
                            or getattr(manager, "string_cache_format_version", 1) != _STRING_CACHE_FORMAT_VERSION
                            or manager._looks_corrupted()
                        ):
                            load_reason = "rebuilt (cache invalid)"
                            manager = build_manager()
                        else:
                            load_reason = "loaded from cache"
                    except Exception:
                        load_reason = "rebuilt (load error)"
                        manager = build_manager()

            log.info(
                "Witcher 2 strings: %s in %.2fs (%d strings)",
                load_reason,
                time.time() - start_time,
                len(manager.Lines),
            )
            W2StringManager.InstanceManagers[manager_key] = manager
            W2StringManager.InstanceManager = manager
            return manager

        W2StringManager.InstanceManager = instance
        return instance
