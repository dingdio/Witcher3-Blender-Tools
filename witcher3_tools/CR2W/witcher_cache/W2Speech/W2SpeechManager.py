from __future__ import annotations

import logging
import os
import pickle
import time

from .. import cache_meta
from .... import dialog_language
from ....extension_paths import get_cache_root
from ..W2Strings.W2Language import normalize_language_handle, language_handle_from_filename
from .W2Speech import W2Speech, W2SpeechParseError

log = logging.getLogger(__name__)


class Configuration:
    VoiceLanguage = "en"
    ExecutablePath = ""


_SPEECH_CACHE_FORMAT_VERSION = 1


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


def current_witcher2_speech_roots():
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


def resolve_w2_speech_roots(override_path: str = "", fallback_roots=None):
    override_path = str(override_path or "").strip()
    if override_path:
        return [override_path]
    if fallback_roots is not None:
        return _coerce_roots(fallback_roots)
    return current_witcher2_speech_roots()


def find_w2_speech_files(roots):
    if roots is None:
        roots = []
    elif isinstance(roots, (str, os.PathLike)):
        roots = [roots]

    found = []
    seen = set()

    def _record(path):
        key = os.path.normcase(os.path.normpath(str(path)))
        if key in seen:
            return
        seen.add(key)
        found.append(str(path))

    def _scan_dir(path):
        if not path or not os.path.isdir(path):
            return
        try:
            entries = sorted(os.listdir(path))
        except Exception:
            return
        for name in entries:
            full_path = os.path.join(path, name)
            if os.path.isfile(full_path) and name.lower().endswith(".w2speech"):
                _record(full_path)

    for root in roots:
        try:
            root_str = str(root)
        except Exception:
            continue
        if not root_str:
            continue
        if os.path.isfile(root_str):
            if root_str.lower().endswith(".w2speech"):
                _record(root_str)
            continue
        if not os.path.isdir(root_str):
            continue

        _scan_dir(root_str)
        _scan_dir(os.path.join(root_str, "CookedPC"))
        _scan_dir(os.path.join(root_str, "data"))
        _scan_dir(os.path.join(root_str, "data", "CookedPC"))

    return found


def find_w2_speech_files_for_path(override_path: str = "", fallback_roots=None):
    return find_w2_speech_files(resolve_w2_speech_roots(override_path, fallback_roots))


def _voice_language(language=None):
    requested = language
    if requested is None:
        try:
            requested = dialog_language.get_active_voice_language()
        except Exception:
            requested = Configuration.VoiceLanguage or "en"
    if not requested:
        requested = Configuration.VoiceLanguage or "en"
    try:
        requested = dialog_language.normalize_dialog_language(requested)
    except Exception:
        pass
    return normalize_language_handle(requested)


def _select_language_files(candidates, language: str):
    handle = normalize_language_handle(language)

    def _for_language(target_handle):
        return [p for p in candidates if language_handle_from_filename(p) == target_handle]

    matched = _for_language(handle)
    used_handle = handle
    if not matched and handle != "en":
        log.info("No %s0.w2speech found; falling back to English.", handle.upper())
        matched = _for_language("en")
        used_handle = "en"
    if not matched and candidates:
        matched = candidates[:1]
        used_handle = language_handle_from_filename(matched[0])
    return matched, used_handle


class W2SpeechManager:
    InstanceManager = None
    InstanceManagers = {}

    def __init__(self):
        self.RequestedLanguage = "en"
        self.Language = "en"
        self.base_path = ""
        self.source_roots = []
        self.cache_files = []

        self.Items = {}
        self.Speeches = {}
        self.FileList = []
        self.HashDict = {}
        self.Extensions = []
        self.AutocompleteSource = []
        self.speech_cache_format_version = _SPEECH_CACHE_FORMAT_VERSION

    def find_item_by_hash(self, hash_value):
        try:
            return self.Items.get(int(hash_value), None)
        except Exception:
            return None

    def LoadBundle(self, filename):
        log.debug("Loading W2 speech bundle: %s", filename)
        if filename in self.Speeches:
            return
        try:
            speech = W2Speech(filename)
        except (W2SpeechParseError, OSError, ValueError) as exc:
            log.warning("Failed to load W2 speech bundle %s: %s", filename, exc)
            return

        self.cache_files.append(filename)
        for item in speech.item_infos:
            self.Items.setdefault(item.id, []).append(item)
            self.HashDict[item.id] = item
            self.FileList.append(item)

        self.Speeches[filename] = speech

    def LoadAll(self, roots, language=None, onlyIfLanguageChanged: bool = False):
        requested_language = _voice_language(language)
        source_roots = resolve_w2_speech_roots(fallback_roots=roots)
        base_key = _roots_cache_key(source_roots)

        if onlyIfLanguageChanged and self.RequestedLanguage == requested_language and self.base_path == base_key:
            return

        self.RequestedLanguage = requested_language
        self.Language = requested_language
        self.base_path = base_key
        self.source_roots = [_normalize_path(str(root)) for root in source_roots if str(root or "").strip()]
        self.cache_files = []
        self.Items = {}
        self.Speeches = {}
        self.FileList = []
        self.HashDict = {}
        self.Extensions = []
        self.AutocompleteSource = []
        self.speech_cache_format_version = _SPEECH_CACHE_FORMAT_VERSION

        candidates = find_w2_speech_files(source_roots)
        if not candidates:
            log.info("Witcher 2 speech cache skipped: no .w2speech files found in %s", self.base_path or "<unset>")
            return

        matched, used_language = _select_language_files(candidates, requested_language)
        self.Language = used_language

        for filename in matched:
            self.LoadBundle(filename)

    @staticmethod
    def BuildSourceSignature(base_path: str = "", language: str = None, roots=None):
        requested_language = _voice_language(language)
        source_roots = resolve_w2_speech_roots(base_path, roots)
        candidates = find_w2_speech_files(source_roots)
        matched, used_language = _select_language_files(candidates, requested_language)
        signature = cache_meta.compute_signature(matched)
        source = {
            "type": "w2speech",
            "requested_language": requested_language,
            "language": used_language,
            "base_path": _roots_cache_key(source_roots),
            "roots": [_normalize_path(str(root)) for root in source_roots if str(root or "").strip()],
            "files": matched,
        }
        return signature, source

    @staticmethod
    def Get(do_reload=False, language=None, game_path: str = "", roots=None):
        requested_language = _voice_language(language)
        source_roots = resolve_w2_speech_roots(game_path, roots)
        current_base_path = _roots_cache_key(source_roots)
        Configuration.VoiceLanguage = requested_language
        Configuration.ExecutablePath = current_base_path

        manager_key = (os.path.normcase(current_base_path or ""), requested_language)
        instance = W2SpeechManager.InstanceManagers.get(manager_key)

        if instance is not None:
            if getattr(instance, "base_path", None) != current_base_path:
                do_reload = True
            if getattr(instance, "RequestedLanguage", "") != requested_language:
                do_reload = True
            if getattr(instance, "speech_cache_format_version", 1) != _SPEECH_CACHE_FORMAT_VERSION:
                do_reload = True

        if instance is None or do_reload:
            cache_root = get_cache_root(create=True)
            cache_dir = os.path.join(cache_root, "W2Speech")
            os.makedirs(cache_dir, exist_ok=True)
            cache_name = f"w2_speech_cache_{requested_language}.pkl"
            cache_path = os.path.join(cache_dir, cache_name)
            meta_path = cache_meta.get_meta_path(cache_path)

            start_time = time.time()
            load_reason = "built"

            def build_manager():
                manager = W2SpeechManager()
                manager.LoadAll(source_roots, requested_language)
                if not manager.cache_files:
                    return manager

                with open(cache_path, "wb") as handle:
                    pickle.dump(manager, handle, protocol=pickle.HIGHEST_PROTOCOL)

                signature, source = W2SpeechManager.BuildSourceSignature(
                    language=requested_language,
                    roots=source_roots,
                )
                meta = cache_meta.make_meta(cache_name, cache_path, signature, source)
                cache_meta.save_meta(meta_path, meta)
                return manager

            if not find_w2_speech_files(source_roots):
                manager = build_manager()
            elif not os.path.exists(cache_path) or do_reload:
                if do_reload:
                    load_reason = "rebuilt (forced)"
                manager = build_manager()
            else:
                meta = cache_meta.load_meta(meta_path)
                current_signature, _source = W2SpeechManager.BuildSourceSignature(
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
                            or getattr(manager, "speech_cache_format_version", 1) != _SPEECH_CACHE_FORMAT_VERSION
                        ):
                            load_reason = "rebuilt (cache invalid)"
                            manager = build_manager()
                        else:
                            load_reason = "loaded from cache"
                    except Exception:
                        load_reason = "rebuilt (load error)"
                        manager = build_manager()

            log.info(
                "Witcher 2 speech: %s in %.2fs (%d entries)",
                load_reason,
                time.time() - start_time,
                len(manager.FileList),
            )
            W2SpeechManager.InstanceManagers[manager_key] = manager
            W2SpeechManager.InstanceManager = manager
            return manager

        W2SpeechManager.InstanceManager = instance
        return instance
