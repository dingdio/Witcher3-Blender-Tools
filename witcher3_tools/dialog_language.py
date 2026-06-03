import logging
import csv
import os
import re
import sqlite3
import time
from pathlib import Path

log = logging.getLogger(__name__)

DIALOG_LEGACY_LANGUAGE_PROP = "witcher_dialog_language"
DIALOG_TEXT_LANGUAGE_PROP = "witcher_dialog_text_language"
DIALOG_VOICE_LANGUAGE_PROP = "witcher_dialog_voice_language"

# Compatibility alias for older callers. New code should use the explicit text
# and voice properties above.
DIALOG_LANGUAGE_PROP = DIALOG_TEXT_LANGUAGE_PROP

DIALOG_SUBTITLE_TEXT_PROP = "witcher_dialog_subtitle_text"
DIALOG_SUBTITLE_LINE_ID_PROP = "witcher_dialog_subtitle_line_id"
DIALOG_SUBTITLE_SPEAKER_PROP = "witcher_dialog_subtitle_speaker"
DIALOG_SUBTITLE_SOURCE_PROP = "witcher_dialog_subtitle_source"
DIALOG_SUBTITLE_SOURCE_PATH_PROP = "witcher_dialog_subtitle_source_path"
DIALOG_SUBTITLE_LANGUAGE_PROP = "witcher_dialog_subtitle_language"
DIALOG_AUDIO_LANGUAGE_PROP = "witcher_dialog_audio_language"

_LANGUAGE_LABELS = {
    "en": "English",
    "de": "German",
    "fr": "French",
    "it": "Italian",
    "es": "Spanish",
    "esMX": "Spanish (Latin America)",
    "pl": "Polish",
    "ru": "Russian",
    "cz": "Czech",
    "hu": "Hungarian",
    "jp": "Japanese",
    "zh": "Chinese Traditional",
    "cn": "Chinese Simplified",
    "br": "Brazilian Portuguese",
    "tr": "Turkish",
    "kr": "Korean",
    "ar": "Arabic",
    "debug": "Debug",
}

_LANGUAGE_LOCALE_IDS = {
    "pl": 1,
    "en": 2,
    "de": 3,
    "it": 4,
    "fr": 5,
    "cz": 6,
    "es": 7,
    "zh": 8,
    "ru": 9,
    "hu": 10,
    "jp": 11,
    "tr": 12,
    "kr": 13,
    "br": 14,
    "esMX": 15,
    "cn": 16,
    "ar": 17,
    "debug": 20,
}

_TEXT_LANGUAGE_ORDER = (
    "en",
    "pl",
    "de",
    "fr",
    "br",
    "ru",
    "jp",
    "it",
    "es",
    "esMX",
    "cz",
    "hu",
    "zh",
    "cn",
    "kr",
    "tr",
    "ar",
)

_VOICE_LANGUAGE_ORDER = (
    "en",
    "pl",
    "de",
    "fr",
    "br",
    "ru",
    "jp",
)

_KNOWN_LANGUAGE_ORDER = tuple(dict.fromkeys(_TEXT_LANGUAGE_ORDER + _VOICE_LANGUAGE_ORDER + tuple(_LANGUAGE_LABELS)))

_W3STRINGS_CACHE = {}
_REDKIT_PROJECT_STRINGS_CACHE = {}
_REDKIT_EDITOR_DB_CACHE = {}
_REDKIT_EDITOR_DB_NAMES = (
    "LocalEditorStringDataBaseW3_UTF8_mod.db",
    "LocalEditorStringDataBaseW3_UTF8.db",
)
_LANGUAGE_SCAN_CACHE = {
    "base_path": None,
    "scanned_at": 0.0,
    "text": tuple(),
    "voice": tuple(),
}
_W2_VOICE_LANGUAGE_SCAN_CACHE = {
    "roots": tuple(),
    "scanned_at": 0.0,
    "voice": tuple(),
}
_LANGUAGE_SCAN_TTL_SECONDS = 30.0
_LANGUAGE_SCAN_IN_PROGRESS = False
_W2_VOICE_LANGUAGE_SCAN_IN_PROGRESS = False

_TEXT_ENUM_ITEMS = []
_VOICE_ENUM_ITEMS = []


def _canonical_language_handle(language):
    value = str(language or "").strip()
    if not value:
        return ""
    compact = value.replace("-", "").replace("_", "")
    lowered = compact.lower()
    aliases = {
        "mx": "esMX",
        "esmx": "esMX",
        "ptbr": "br",
        "pt": "br",
        "ja": "jp",
        "jp": "jp",
        "cs": "cz",
        "cz": "cz",
        "ko": "kr",
        "cn": "cn",
        "zhcn": "cn",
        "zhs": "cn",
        "zhhans": "cn",
        "zh": "zh",
        "zhhk": "zh",
        "zhtw": "zh",
        "zhhant": "zh",
    }
    if lowered in aliases:
        return aliases[lowered]
    for handle in _KNOWN_LANGUAGE_ORDER:
        if handle.lower() == lowered:
            return handle
    return value


def language_label(language):
    handle = normalize_dialog_language(language)
    return _LANGUAGE_LABELS.get(handle, handle)


def language_locale_id(language):
    return _LANGUAGE_LOCALE_IDS.get(normalize_dialog_language(language))


def _ordered_languages(handles, preferred_order):
    seen = set()
    ordered = []
    handle_set = {normalize_dialog_language(handle) for handle in handles if handle}
    for handle in preferred_order:
        if handle in handle_set and handle not in seen:
            ordered.append(handle)
            seen.add(handle)
    for handle in sorted(handle_set, key=lambda item: item.lower()):
        if handle not in seen:
            ordered.append(handle)
            seen.add(handle)
    return tuple(ordered)


def _get_game_base_path():
    return _get_addon_pref_path("witcher_game_path")


def _get_addon_pref_path(attr_name):
    try:
        import bpy

        prefs_root = getattr(getattr(bpy, "context", None), "preferences", None)
        addons = getattr(prefs_root, "addons", None)
        if addons:
            package_root = (__package__ or __name__).split(".")[0]
            addon_entry = None
            try:
                addon_entry = addons.get(package_root) if hasattr(addons, "get") else addons[package_root]
            except Exception:
                addon_entry = None
            if addon_entry is None:
                for key in getattr(addons, "keys", lambda: [])():
                    if str(key).split(".")[0] == package_root:
                        addon_entry = addons[key]
                        break
            prefs = getattr(addon_entry, "preferences", None) if addon_entry is not None else None
            game_path = str(getattr(prefs, attr_name, "") or "").strip()
            if game_path:
                return os.path.normpath(os.path.abspath(game_path))
    except Exception:
        pass
    return ""


def _language_scan_roots(base_path):
    roots = []
    if not base_path or not os.path.isdir(base_path):
        return roots

    for subdir in ("content", "dlc", "mods", "mod", "DLC", "MOD"):
        folder = os.path.join(base_path, subdir)
        if os.path.isdir(folder):
            roots.append(folder)
    roots.append(base_path)
    return roots


def _w2_voice_scan_roots():
    roots = []
    for attr in ("witcher2_game_path", "w2_unbundle_path"):
        path = _get_addon_pref_path(attr)
        if not path:
            continue
        for candidate in (
            path,
            os.path.join(path, "CookedPC"),
            os.path.join(path, "data"),
            os.path.join(path, "data", "CookedPC"),
        ):
            if candidate and os.path.isdir(candidate):
                roots.append(os.path.normpath(os.path.abspath(candidate)))

    unique = []
    seen = set()
    for root in roots:
        key = os.path.normcase(root)
        if key in seen:
            continue
        seen.add(key)
        unique.append(root)
    return tuple(unique)


def _scan_installed_languages(force=False):
    global _LANGUAGE_SCAN_IN_PROGRESS
    base_path = _get_game_base_path()
    now = time.time()
    if (
        not force
        and _LANGUAGE_SCAN_CACHE.get("base_path") == base_path
        and now - float(_LANGUAGE_SCAN_CACHE.get("scanned_at") or 0.0) < _LANGUAGE_SCAN_TTL_SECONDS
    ):
        return _LANGUAGE_SCAN_CACHE["text"], _LANGUAGE_SCAN_CACHE["voice"]
    if _LANGUAGE_SCAN_IN_PROGRESS:
        return _LANGUAGE_SCAN_CACHE["text"], _LANGUAGE_SCAN_CACHE["voice"]
    if not base_path:
        _LANGUAGE_SCAN_CACHE.update({
            "base_path": base_path,
            "scanned_at": now,
            "text": tuple(),
            "voice": tuple(),
        })
        return tuple(), tuple()

    text_languages = set()
    voice_languages = set()
    speech_pattern = re.compile(r"^(.+?)pc\.w3speech$", re.IGNORECASE)

    _LANGUAGE_SCAN_IN_PROGRESS = True
    try:
        for root in _language_scan_roots(base_path):
            try:
                if os.path.normcase(os.path.abspath(root)) == os.path.normcase(os.path.abspath(base_path)):
                    iterable = [(root, [], sorted(os.listdir(root)))]
                else:
                    iterable = os.walk(root)
                for dirpath, dirnames, filenames in iterable:
                    dirnames.sort()
                    for filename in sorted(filenames):
                        if not os.path.isfile(os.path.join(dirpath, filename)):
                            continue
                        lower_name = filename.lower()
                        if lower_name.endswith(".w3strings"):
                            text_languages.add(_canonical_language_handle(os.path.splitext(filename)[0]))
                            continue

                        match = speech_pattern.match(filename)
                        if match:
                            voice_languages.add(_canonical_language_handle(match.group(1)))
            except Exception:
                pass
    finally:
        _LANGUAGE_SCAN_IN_PROGRESS = False

    text = _ordered_languages(text_languages, _TEXT_LANGUAGE_ORDER)
    voice = _ordered_languages(voice_languages, _VOICE_LANGUAGE_ORDER)
    _LANGUAGE_SCAN_CACHE.update({
        "base_path": base_path,
        "scanned_at": now,
        "text": text,
        "voice": voice,
    })
    return text, voice


def _scan_installed_w2_voice_languages(force=False):
    global _W2_VOICE_LANGUAGE_SCAN_IN_PROGRESS
    roots = _w2_voice_scan_roots()
    now = time.time()
    if (
        not force
        and _W2_VOICE_LANGUAGE_SCAN_CACHE.get("roots") == roots
        and now - float(_W2_VOICE_LANGUAGE_SCAN_CACHE.get("scanned_at") or 0.0) < _LANGUAGE_SCAN_TTL_SECONDS
    ):
        return _W2_VOICE_LANGUAGE_SCAN_CACHE["voice"]
    if _W2_VOICE_LANGUAGE_SCAN_IN_PROGRESS:
        return _W2_VOICE_LANGUAGE_SCAN_CACHE["voice"]
    if not roots:
        _W2_VOICE_LANGUAGE_SCAN_CACHE.update({
            "roots": roots,
            "scanned_at": now,
            "voice": tuple(),
        })
        return tuple()

    voice_languages = set()
    speech_pattern = re.compile(r"^(.+?)0?\.w2speech$", re.IGNORECASE)
    _W2_VOICE_LANGUAGE_SCAN_IN_PROGRESS = True
    try:
        for root in roots:
            try:
                for filename in sorted(os.listdir(root)):
                    path = os.path.join(root, filename)
                    if not os.path.isfile(path):
                        continue
                    match = speech_pattern.match(filename)
                    if match:
                        voice_languages.add(_canonical_language_handle(match.group(1)))
            except Exception:
                pass
    finally:
        _W2_VOICE_LANGUAGE_SCAN_IN_PROGRESS = False

    voice = _ordered_languages(voice_languages, _VOICE_LANGUAGE_ORDER)
    _W2_VOICE_LANGUAGE_SCAN_CACHE.update({
        "roots": roots,
        "scanned_at": now,
        "voice": voice,
    })
    return voice


def refresh_language_capability_cache():
    text, w3_voice = _scan_installed_languages(force=True)
    w2_voice = _scan_installed_w2_voice_languages(force=True)
    voice = _ordered_languages(tuple(w3_voice) + tuple(w2_voice), _VOICE_LANGUAGE_ORDER)
    return text, voice


def installed_text_languages():
    text, _voice = _scan_installed_languages()
    return text


def installed_voice_languages():
    _text, w3_voice = _scan_installed_languages()
    w2_voice = _scan_installed_w2_voice_languages()
    return _ordered_languages(tuple(w3_voice) + tuple(w2_voice), _VOICE_LANGUAGE_ORDER)


def has_installed_voice_language(language):
    return normalize_dialog_language(language) in set(installed_voice_languages())


def supported_dialog_languages():
    """Compatibility helper: return supported text/subtitle language handles."""
    return list(_TEXT_LANGUAGE_ORDER)


def supported_voice_languages():
    return list(_VOICE_LANGUAGE_ORDER)


def _scene_language_value(context, prop_name, default=""):
    scene = getattr(context, "scene", None) if context is not None else None
    if scene is None:
        try:
            import bpy
            scene = getattr(bpy.context, "scene", None)
        except Exception:
            scene = None
    if scene is None:
        return default
    try:
        value = getattr(scene, prop_name)
    except Exception:
        try:
            value = scene.get(prop_name, default)
        except Exception:
            value = default
    return value or default


def _language_enum_items(kind, context=None):
    if kind == "voice":
        handles = list(supported_voice_languages())
        active = _scene_language_value(context, DIALOG_VOICE_LANGUAGE_PROP, "")
        description = "Use this language for speech audio and lipsync imports"
        preferred_order = _VOICE_LANGUAGE_ORDER
        scene = getattr(context, "scene", None) if context is not None else None
        voice_game = str(getattr(scene, "witcher_voice_game", "") or "").upper() if scene is not None else ""
        if voice_game == "W2":
            installed_voice = set(_scan_installed_w2_voice_languages())
            install_status_known = bool(_w2_voice_scan_roots())
        elif voice_game == "W3":
            _text, w3_voice = _scan_installed_languages()
            installed_voice = set(w3_voice)
            w3_base_path = _get_game_base_path()
            install_status_known = bool(w3_base_path and os.path.isdir(w3_base_path))
        else:
            installed_voice = set(installed_voice_languages())
            w3_base_path = _get_game_base_path()
            install_status_known = bool((w3_base_path and os.path.isdir(w3_base_path)) or _w2_voice_scan_roots())
    else:
        handles = list(supported_dialog_languages())
        active = (
            _scene_language_value(context, DIALOG_TEXT_LANGUAGE_PROP, "")
            or _scene_language_value(context, DIALOG_LEGACY_LANGUAGE_PROP, "")
        )
        description = "Use this language for dialog text and viewport subtitles"
        preferred_order = _TEXT_LANGUAGE_ORDER
        installed_voice = set()
        install_status_known = False

    active = normalize_dialog_language(active) if active else ""
    if active and active != "0" and active not in handles:
        handles.append(active)
    if "en" not in handles:
        handles.append("en")

    items = []
    for handle in _ordered_languages(handles, preferred_order):
        label = f"{handle.upper()} - {_LANGUAGE_LABELS.get(handle, handle)}"
        item_description = description
        if kind == "voice" and install_status_known:
            if handle in installed_voice:
                item_description = f"{description}. Voice pack detected."
            else:
                label = f"{label} (not installed)"
                item_description = f"{description}. Voice pack was not detected; imports will fall back when possible."
        items.append((handle, label, item_description))
    return items


def dialog_text_language_enum_items(self=None, context=None):
    global _TEXT_ENUM_ITEMS
    _TEXT_ENUM_ITEMS = _language_enum_items("text", context)
    return _TEXT_ENUM_ITEMS


def dialog_voice_language_enum_items(self=None, context=None):
    global _VOICE_ENUM_ITEMS
    _VOICE_ENUM_ITEMS = _language_enum_items("voice", context)
    return _VOICE_ENUM_ITEMS


def dialog_language_enum_items(_self=None, context=None):
    return dialog_text_language_enum_items(_self, context)


def normalize_dialog_language(language):
    language = _canonical_language_handle(language)
    if not language:
        return "en"
    if language in _LANGUAGE_LABELS or language in _LANGUAGE_LOCALE_IDS:
        return language
    lowered = language.lower()
    for handle in _KNOWN_LANGUAGE_ORDER:
        if handle.lower() == lowered:
            return handle
    return language


def _get_config_text_language(default="en"):
    try:
        from .CR2W.witcher_cache.W3Strings.W3StringManager import Configuration
        return normalize_dialog_language(getattr(Configuration, "TextLanguage", "") or default)
    except Exception:
        return normalize_dialog_language(default)


def _get_config_voice_language(default="en"):
    try:
        from .CR2W.witcher_cache.W3Strings.W3StringManager import Configuration
        return normalize_dialog_language(getattr(Configuration, "VoiceLanguage", "") or default)
    except Exception:
        return normalize_dialog_language(default)


def get_active_text_language(context=None, default="en"):
    language = _scene_language_value(context, DIALOG_TEXT_LANGUAGE_PROP, "")
    if not language:
        language = _scene_language_value(context, DIALOG_LEGACY_LANGUAGE_PROP, "")
    return normalize_dialog_language(language or _get_config_text_language(default))


def get_active_voice_language(context=None, default="en"):
    language = _scene_language_value(context, DIALOG_VOICE_LANGUAGE_PROP, "")
    return normalize_dialog_language(language or _get_config_voice_language(default))


def get_active_dialog_language(context=None, default="en"):
    return get_active_text_language(context, default=default)


def resolve_voice_language(language=None, context=None, fallback="en"):
    requested = normalize_dialog_language(language or get_active_voice_language(context, default=fallback))
    available = list(installed_voice_languages())
    if not available:
        return requested
    if requested in available:
        return requested
    fallback = normalize_dialog_language(fallback)
    if fallback in available:
        return fallback
    if "en" in available:
        return "en"
    return available[0]


def set_active_dialog_languages(text_language=None, voice_language=None, reset_string_manager=True):
    text_language = normalize_dialog_language(text_language or get_active_text_language())
    voice_language = normalize_dialog_language(voice_language or get_active_voice_language(default=text_language))
    try:
        from .CR2W.witcher_cache.W3Strings.W3StringManager import Configuration, W3StringManager

        old_text_language = normalize_dialog_language(getattr(Configuration, "TextLanguage", "en"))
        Configuration.TextLanguage = text_language
        Configuration.VoiceLanguage = voice_language
        if reset_string_manager and old_text_language != text_language:
            W3StringManager.InstanceManager = None
            _W3STRINGS_CACHE.clear()
    except Exception:
        pass
    return text_language, voice_language


def set_active_text_language(language, reset_string_manager=True):
    text_language = normalize_dialog_language(language)
    set_active_dialog_languages(
        text_language=text_language,
        voice_language=get_active_voice_language(default=text_language),
        reset_string_manager=reset_string_manager,
    )
    return text_language


def set_active_voice_language(language):
    voice_language = normalize_dialog_language(language)
    set_active_dialog_languages(
        text_language=get_active_text_language(default=voice_language),
        voice_language=voice_language,
        reset_string_manager=False,
    )
    return voice_language


def set_active_dialog_language(language, reset_string_manager=True):
    return set_active_text_language(language, reset_string_manager=reset_string_manager)


def localized_string_id(prop):
    string_obj = getattr(prop, "String", None) if prop is not None else None
    if string_obj is None:
        return ""
    return str(getattr(string_obj, "val", "") or "").strip()


def _source_root_candidates(source_filepath):
    source_path = str(source_filepath or "").strip().replace("/", "\\")
    if not source_path or not os.path.isabs(source_path):
        return []

    roots = []
    normalized = os.path.normpath(source_path)
    lowered = normalized.lower()
    for marker in ("\\r4data\\", "\\workspace\\", "\\content\\content0\\"):
        marker_idx = lowered.find(marker)
        if marker_idx >= 0:
            root = normalized[:marker_idx + len(marker) - 1]
            if os.path.isdir(root):
                roots.append(root)
    return roots


def _load_w3strings_map(strings_path):
    strings_path = os.path.normpath(str(strings_path or ""))
    if not strings_path:
        return {}
    try:
        mtime = os.path.getmtime(strings_path)
    except OSError:
        return {}

    strings_key = (os.path.normcase(strings_path), mtime)
    cached = _W3STRINGS_CACHE.get(strings_key)
    if cached is not None:
        return cached

    lines = {}
    try:
        from .CR2W.witcher_cache.W3Strings.W3StringFile import W3StringFile
        from .CR2W.bStream import bStream

        string_file = W3StringFile()
        with open(strings_path, "rb") as reader:
            stream = bStream(path=strings_path, reader=reader)
            string_file.Read(stream)
        for item in string_file.block1:
            lines[int(item.str_id)] = item.str
    except Exception:
        log.debug("Could not read dialog string table: %s", strings_path, exc_info=True)

    _W3STRINGS_CACHE[strings_key] = lines
    return lines


def _resolve_from_source_table(line_key, source_filepath, language):
    for root in _source_root_candidates(source_filepath):
        strings_path = os.path.join(root, f"{language}.w3strings")
        if os.path.isfile(strings_path):
            text = _load_w3strings_map(strings_path).get(line_key, "")
            if text:
                return text
    return ""


def _redkit_project_string_roots(source_filepath=""):
    roots = []

    source_path = str(source_filepath or "").strip()
    if source_path and os.path.isabs(source_path):
        try:
            path = Path(source_path)
            parent = path if path.is_dir() else path.parent
            for candidate in (parent, *parent.parents):
                if (candidate / "LocalEditorStringDataBaseW3_UTF8_mod_export.csv").is_file():
                    roots.append(candidate)
                    break
        except Exception:
            log.debug("Could not inspect REDkit string roots for %s", source_filepath, exc_info=True)

    try:
        import bpy
        from . import get_all_addon_prefs

        prefs = get_all_addon_prefs(getattr(bpy, "context", None))
        for item in getattr(prefs, "redkit_projects", []) or []:
            path = str(getattr(item, "path", "") or "").strip()
            if not path:
                continue
            try:
                path = bpy.path.abspath(path)
            except Exception:
                pass
            candidate = Path(os.path.normpath(path))
            if (candidate / "LocalEditorStringDataBaseW3_UTF8_mod_export.csv").is_file():
                roots.append(candidate)
    except Exception:
        pass

    unique = []
    seen = set()
    for root in roots:
        key = os.path.normcase(os.path.normpath(str(root)))
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def _load_redkit_project_strings_map(project_root, language):
    try:
        from .lipsync import redkit_project

        csv_name = redkit_project.PROJECT_STRINGS_CSV
        language_column = redkit_project._language_column(language)
    except Exception:
        csv_name = "LocalEditorStringDataBaseW3_UTF8_mod_export.csv"
        language_column = str(language or "en").strip().upper() or "EN"

    csv_path = Path(project_root) / csv_name
    try:
        mtime = csv_path.stat().st_mtime
    except OSError:
        return {}

    cache_key = (os.path.normcase(os.path.normpath(str(csv_path))), mtime, language_column)
    cached = _REDKIT_PROJECT_STRINGS_CACHE.get(cache_key)
    if cached is not None:
        return cached

    strings = {}
    try:
        with open(csv_path, "r", encoding="utf-8-sig", errors="replace", newline="") as handle:
            reader = csv.DictReader(handle, delimiter=";")
            for row in reader:
                try:
                    line_key = int(str(row.get("ID", "") or "").strip())
                except (TypeError, ValueError):
                    continue
                text = str(row.get(language_column, "") or row.get("EN", "") or "").strip()
                if text:
                    strings[line_key] = text
    except Exception:
        log.debug("Could not read REDkit project strings: %s", csv_path, exc_info=True)

    _REDKIT_PROJECT_STRINGS_CACHE[cache_key] = strings
    return strings


def _resolve_from_redkit_project_strings(line_key, source_filepath, language):
    for root in _redkit_project_string_roots(source_filepath):
        text = _load_redkit_project_strings_map(root, language).get(line_key, "")
        if text:
            return text
    return ""


def _redkit_editor_db_roots(source_filepath=""):
    roots = []

    source_path = str(source_filepath or "").strip()
    if source_path and os.path.isabs(source_path):
        try:
            path = Path(source_path)
            parent = path if path.is_dir() else path.parent
            for candidate in (parent, *parent.parents):
                if any((candidate / db_name).is_file() for db_name in _REDKIT_EDITOR_DB_NAMES):
                    roots.append(candidate)
                    break
        except Exception:
            log.debug("Could not inspect REDkit DB roots for %s", source_filepath, exc_info=True)

    for root in _source_root_candidates(source_filepath):
        candidate = Path(root)
        if any((candidate / db_name).is_file() for db_name in _REDKIT_EDITOR_DB_NAMES):
            roots.append(candidate)

    try:
        import bpy
        from . import get_all_addon_prefs

        prefs = get_all_addon_prefs(getattr(bpy, "context", None))
        depot_path = str(getattr(prefs, "redkit_depot_path", "") or "").strip()
        if depot_path:
            try:
                depot_path = bpy.path.abspath(depot_path)
            except Exception:
                pass
            candidate = Path(os.path.normpath(depot_path))
            if any((candidate / db_name).is_file() for db_name in _REDKIT_EDITOR_DB_NAMES):
                roots.append(candidate)
    except Exception:
        pass

    unique = []
    seen = set()
    for root in roots:
        key = os.path.normcase(os.path.normpath(str(root)))
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def _redkit_editor_db_language_chain(cursor, language):
    rows = cursor.execute("SELECT ID, LANG, FALLBACK FROM LANGUAGES").fetchall()
    id_by_lang = {str(lang or "").strip().upper(): lang_id for lang_id, lang, _fallback in rows}
    fallback_by_id = {lang_id: fallback for lang_id, _lang, fallback in rows}

    requested = str(language or "en").strip().upper()
    start_id = id_by_lang.get(requested) or id_by_lang.get("EN")
    chain = []
    seen = set()
    current_id = start_id
    while current_id and current_id not in seen:
        seen.add(current_id)
        chain.append(current_id)
        current_id = fallback_by_id.get(current_id)

    for fallback_lang in ("EN", "PL", "DEBUG"):
        fallback_id = id_by_lang.get(fallback_lang)
        if fallback_id and fallback_id not in seen:
            chain.append(fallback_id)
            seen.add(fallback_id)
    return chain


def _query_redkit_editor_db(db_path, line_key, language):
    db_path = Path(db_path)
    try:
        mtime = db_path.stat().st_mtime
    except OSError:
        return ""

    cache_key = (
        os.path.normcase(os.path.normpath(str(db_path))),
        mtime,
        normalize_dialog_language(language),
        int(line_key),
    )
    if cache_key in _REDKIT_EDITOR_DB_CACHE:
        return _REDKIT_EDITOR_DB_CACHE[cache_key]

    text = ""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
        try:
            cursor = conn.cursor()
            for lang_id in _redkit_editor_db_language_chain(cursor, language):
                row = cursor.execute(
                    """
                    SELECT TEXT
                    FROM LATEST_STRINGS
                    WHERE STRING_ID = ? AND LANG = ? AND TEXT IS NOT NULL AND TRIM(TEXT) != ''
                    ORDER BY VERSION DESC
                    LIMIT 1
                    """,
                    (int(line_key), int(lang_id)),
                ).fetchone()
                if row and str(row[0] or "").strip():
                    text = str(row[0] or "").strip()
                    break
        finally:
            conn.close()
    except Exception:
        log.debug("Could not query REDkit editor string DB: %s", db_path, exc_info=True)

    _REDKIT_EDITOR_DB_CACHE[cache_key] = text
    return text


def _resolve_from_redkit_editor_dbs(line_key, source_filepath, language):
    for root in _redkit_editor_db_roots(source_filepath):
        for db_name in _REDKIT_EDITOR_DB_NAMES:
            text = _query_redkit_editor_db(root / db_name, line_key, language)
            if text:
                return text
    return ""


def _resolve_from_string_manager(line_key, language):
    try:
        from .CR2W.witcher_cache.W3Strings import LoadStringsManager
        from .CR2W.witcher_cache.W3Strings.W3StringManager import W3StringManager

        manager_language = normalize_dialog_language(getattr(W3StringManager.InstanceManager, "Language", "") or "")
        set_active_text_language(language, reset_string_manager=manager_language != language)
        string_manager = LoadStringsManager()
        return str(string_manager.GetString(line_key) or "")
    except Exception:
        log.debug("Could not resolve dialog string %s from W3StringManager.", line_key, exc_info=True)
    return ""


def _resolve_from_w2_string_manager(line_key, language):
    try:
        from .CR2W.witcher_cache.W2Strings import LoadWitcher2StringsManager

        string_manager = LoadWitcher2StringsManager(language=language)
        return str(string_manager.GetString(line_key) or "")
    except Exception:
        log.debug("Could not resolve dialog string %s from W2StringManager.", line_key, exc_info=True)
    return ""


def resolve_localized_text(line_id, source_filepath="", language=None, source_game=""):
    try:
        line_key = int(str(line_id or "").strip())
    except (TypeError, ValueError):
        return ""
    if not line_key:
        return ""

    language = normalize_dialog_language(language or get_active_text_language())
    if str(source_game or "").upper() == "W2":
        return _resolve_from_w2_string_manager(line_key, language)

    text = _resolve_from_redkit_project_strings(line_key, source_filepath, language)
    if text:
        return text
    text = _resolve_from_source_table(line_key, source_filepath, language)
    if text:
        return text
    text = _resolve_from_string_manager(line_key, language)
    if text:
        return text
    return _resolve_from_redkit_editor_dbs(line_key, source_filepath, language)
