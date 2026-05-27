"""Data sources for the Witcher 2/3 Strings Browser.

Builds a flat list of StringRecord dicts from:

  * The Witcher 3 binary ``*.w3strings`` cache (via W3StringManager)
  * The Witcher 2 binary ``*.w2strings`` cache (via W2StringManager)
  * The Witcher 3 REDkit SQLite DB (``LocalEditorStringDataBaseW3_UTF8*.db``)
  * The Witcher 2 REDkit SQLite DB (``LocalEditorStringDataBaseW2_UTF8*.db``)

Speaker names are resolved by reusing the Dialogue Browser data sources
(``voice_names.json`` + ``actor_voicelines.csv``) and falling back to
``STRING_INFO.VOICEOVER_NAME`` derived speaker strings.

The browser uses these records read-only (browse only, no editing).
"""

from __future__ import annotations

import csv
import json
import logging
import os
import sqlite3
import time
from pathlib import Path

log = logging.getLogger(__name__)


GAME_W3 = "W3"
GAME_W2 = "W2"

SOURCE_W3STRINGS = "w3strings"
SOURCE_W2STRINGS = "w2strings"
SOURCE_SQLITE = "sqlite"


W3_REDKIT_DB_NAMES = (
    "LocalEditorStringDataBaseW3_UTF8.db",
    "LocalEditorStringDataBaseW3_UTF8_mod.db",
)

W2_REDKIT_DB_NAMES = (
    # Witcher 2 ships its strings DB as base.sqlite + user.sqlite inside the
    # game's bin/ folder. user.sqlite is the writable mod overlay; for a fresh
    # REDkit install it has 1 row, while base.sqlite holds the shipped 64K+
    # strings. We always sort the final candidate list by file size descending
    # so the populated DB wins regardless of filename order here.
    "base.sqlite",
    "user.sqlite",
)

# Subdirectories worth probing under any candidate root. r4data is where the
# Witcher 3 REDkit keeps its DB; bin is the Witcher 2 install layout.
_DB_SUBDIRS = ("r4data", "bin")


# Maximum number of records assembled per build. The full W3 string table is
# around 64k rows; the SQLite DB can have more. We cap to keep the UI responsive
# without paging the underlying list (paging is applied at the UI layer).
MAX_RECORDS_PER_SOURCE = 200000


_SPEAKER_CACHE = {"voice_names": None, "csv_speakers": None, "speaker_codes": None}


# ---------------------------------------------------------------------------
# Public record shape
# ---------------------------------------------------------------------------

def make_record(
    *,
    game,
    source,
    string_id,
    string_key="",
    text="",
    voiceover="",
    speaker="",
    speaker_code="",
    resource="",
    property_name="",
    language="",
    db_path="",
):
    """Return the canonical StringRecord dict consumed by the UI list."""

    sid_int = 0
    sid_str = ""
    try:
        sid_int = int(string_id)
        sid_str = str(sid_int)
    except (TypeError, ValueError):
        sid_str = str(string_id or "").strip()

    text = str(text or "")
    speaker = str(speaker or "").strip()
    speaker_code = str(speaker_code or "").strip().upper()
    voiceover = str(voiceover or "").strip()
    string_key = str(string_key or "").strip()
    resource = str(resource or "").strip()
    property_name = str(property_name or "").strip()

    # search blob is everything lowercased — built once at record creation so
    # the inner search loop is just substring matching. We include the raw
    # 4-letter code so searches like ``grlt`` still match Geralt's lines.
    blob_parts = [sid_str, string_key, voiceover, speaker, speaker_code, text]
    search_blob = " ".join(part for part in blob_parts if part).lower()

    return {
        "game": str(game or "").upper(),
        "source": str(source or "").lower(),
        "string_id": sid_int,
        "string_id_str": sid_str,
        "string_key": string_key,
        "text": text,
        "voiceover": voiceover,
        # ``speaker`` is the uppercase resolved display name — used as the
        # equality key by the filter. ``speaker_display`` is what the UI
        # renders. ``speaker_code`` keeps the raw 4-letter abbreviation
        # (e.g. ``GRLT``) for diagnostic display next to the resolved name.
        "speaker": speaker.upper(),
        "speaker_display": speaker,
        "speaker_code": speaker_code,
        "resource": resource,
        "property": property_name,
        "language": str(language or "").lower(),
        "db_path": str(db_path or ""),
        "search_blob": search_blob,
    }


# ---------------------------------------------------------------------------
# Speaker resolution (reuses Dialogue Browser inputs)
# ---------------------------------------------------------------------------

def _voice_names_path():
    try:
        base = Path(__file__).resolve().parents[1]
        return str(base / "CR2W" / "data" / "voice_names.json")
    except Exception:
        return ""


def _actor_voicelines_path():
    try:
        base = Path(__file__).resolve().parents[1]
        return str(base / "CR2W" / "data" / "actor_voicelines.csv")
    except Exception:
        return ""


def _speaker_codes_path():
    try:
        base = Path(__file__).resolve().parents[1]
        return str(base / "CR2W" / "data" / "speaker_codes.json")
    except Exception:
        return ""


def _load_speaker_codes_map():
    """Load the 4-letter-code → full-name reverse map from speaker_codes.json.

    The file lives at ``CR2W/data/speaker_codes.json`` and is community-
    extendable: new mappings can be added without touching the code. Keys
    starting with an underscore are treated as comments and skipped.
    """
    cached = _SPEAKER_CACHE.get("speaker_codes")
    if cached is not None:
        return cached
    out = {}
    path = _speaker_codes_path()
    if path and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                for key, value in data.items():
                    if not isinstance(key, str) or key.startswith("_"):
                        continue
                    if not isinstance(value, str) or not value.strip():
                        continue
                    out[key.strip().lower()] = value.strip()
        except Exception:
            log.debug("Could not read speaker_codes.json", exc_info=True)
    _SPEAKER_CACHE["speaker_codes"] = out
    return out


def _load_voice_names_json():
    cached = _SPEAKER_CACHE.get("voice_names")
    if cached is not None:
        return cached
    out = {}
    path = _voice_names_path()
    if path and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                for key, value in data.items():
                    out[str(key)] = str(value or "").strip()
        except Exception:
            log.debug("Could not read voice_names.json", exc_info=True)
    _SPEAKER_CACHE["voice_names"] = out
    return out


def _load_csv_speaker_map():
    cached = _SPEAKER_CACHE.get("csv_speakers")
    if cached is not None:
        return cached
    out = {}
    path = _actor_voicelines_path()
    if path and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                reader = csv.DictReader(handle, delimiter=";")
                for row in reader:
                    sid = str(row.get("ID", "") or "").strip()
                    if not sid:
                        continue
                    speaker = _csv_fallback_speaker(row)
                    if speaker:
                        out[sid] = speaker
        except Exception:
            log.debug("Could not read actor_voicelines.csv", exc_info=True)
    _SPEAKER_CACHE["csv_speakers"] = out
    return out


def _csv_fallback_speaker(row):
    # Same heuristic as ui_voice._fallback_speaker_from_csv_row — picks the
    # first CAT column that does not look like a structural group tag.
    cat1 = (row.get("CAT1") or "").strip()
    cat2 = (row.get("CAT2") or "").strip()
    cat3 = (row.get("CAT3") or "").strip()
    group_prefixes = (
        "group", "grp", "scene", "section", "part", "line", "set",
        "block", "node", "choice", "variant", "state", "phase",
    )
    campaign_tags = {"bob", "ep1"}

    def looks_group(value):
        v = (value or "").strip().lower()
        if not v:
            return False
        for prefix in group_prefixes:
            if v == prefix:
                return True
            if v.startswith(prefix) and v[len(prefix):].isdigit():
                return True
        return False

    candidates = [cat2, cat1, cat3]
    for candidate in candidates:
        if not candidate:
            continue
        if looks_group(candidate):
            continue
        if candidate.lower() in campaign_tags:
            continue
        return _format_speaker_display(candidate)
    for candidate in candidates:
        if candidate and not looks_group(candidate):
            return _format_speaker_display(candidate)
    return ""


def _format_speaker_display(value):
    cleaned = (value or "").strip().replace("_", " ")
    if not cleaned:
        return ""
    return " ".join(part.capitalize() for part in cleaned.split())


def _speaker_code_from_voiceover(voiceover, line_id):
    """Return just the speaker abbreviation chunk of a VOICEOVER_NAME.

    REDkit voiceover ids follow ``<SPEAKER>_<CATEGORY>_<ID>`` (e.g.
    ``GRLT_GC_152469``, ``TRSS_MT_00153025``, ``SHPK_TEMPLATE_30262``). The
    first underscore-separated chunk is the speaker abbreviation; everything
    after it is category + line id and is not part of the speaker name.
    """
    voiceover = str(voiceover or "").strip()
    line_id = str(line_id or "").strip()
    if not voiceover:
        return ""

    parts = voiceover.split("_")
    # Strip trailing-id and trailing-category components so we get the leading
    # speaker chunk. The chunk may itself contain underscores (e.g.
    # ``anna_henrieta`` from older datasets), but for the REDkit voiceover
    # format the first chunk is always pure letters.
    if len(parts) >= 3 and parts[-1].lstrip("0").isdigit():
        return parts[0]
    if line_id and voiceover.upper().endswith("_" + line_id.upper()):
        # ``<head>_<line_id>`` — head is the speaker stem.
        head = voiceover[: -(len(line_id) + 1)]
        return head.split("_", 1)[0] if "_" in head else head
    return parts[0] if parts else voiceover


def _speaker_from_voiceover(voiceover, line_id):
    """Resolve a VOICEOVER_NAME to a display name via the reverse-map.

    Order of preference:

    1. The full first-chunk lookup in ``speaker_codes.json`` (handles
       ``GRLT`` → ``Geralt``).
    2. The full pre-id portion (handles multi-chunk codes like
       ``anna_henrieta`` that some W3 voiceover ids still use).
    3. Title-cased fallback (so an unmapped ``XYZ`` still shows as ``Xyz``).
    """
    code = _speaker_code_from_voiceover(voiceover, line_id)
    if not code:
        return ""

    codes_map = _load_speaker_codes_map()
    resolved = codes_map.get(code.lower())
    if resolved:
        return resolved

    voiceover = str(voiceover or "").strip()
    line_id = str(line_id or "").strip()
    if line_id and voiceover.upper().endswith("_" + line_id.upper()):
        head = voiceover[: -(len(line_id) + 1)]
        head_resolved = codes_map.get(head.lower())
        if head_resolved:
            return head_resolved

    return code


def resolve_speaker(string_id, voiceover):
    """Speaker name resolution shared by every source.

    Priority:

    1. ``voice_names.json`` — maps voice-line id to a short speaker code.
       The code is then translated to a full name via the reverse map.
    2. ``actor_voicelines.csv`` — already-Title-cased character names.
    3. The voiceover column on STRING_INFO (``GRLT_GC_152469`` → ``Geralt``).
    """

    sid_str = str(string_id or "").strip()
    codes_map = _load_speaker_codes_map()

    # voice_names.json gives a short speaker code per line id.
    voice_names = _load_voice_names_json()
    candidate = voice_names.get(sid_str)
    if candidate:
        resolved = codes_map.get(str(candidate).strip().lower())
        if resolved:
            return resolved
        return _format_speaker_display(candidate)

    # actor_voicelines.csv is already a display name.
    csv_map = _load_csv_speaker_map()
    candidate = csv_map.get(sid_str)
    if candidate:
        # Even Title-cased values may still match a code-map entry; check.
        resolved = codes_map.get(str(candidate).strip().lower())
        if resolved:
            return resolved
        return candidate

    # Fall back to deriving a code from the voiceover column.
    return _speaker_from_voiceover(voiceover, sid_str)


def clear_speaker_cache():
    _SPEAKER_CACHE["voice_names"] = None
    _SPEAKER_CACHE["csv_speakers"] = None
    _SPEAKER_CACHE["speaker_codes"] = None


# ---------------------------------------------------------------------------
# SQLite path auto-detection
# ---------------------------------------------------------------------------

def _exists(path):
    try:
        return os.path.isfile(path)
    except Exception:
        return False


def _iter_parents(start):
    seen = set()
    try:
        candidate = Path(start)
    except Exception:
        return
    parent = candidate if candidate.is_dir() else candidate.parent
    for current in (parent, *parent.parents):
        key = str(current).lower()
        if key in seen:
            continue
        seen.add(key)
        yield current


def _scan_for_db(root, db_names, max_depth=2):
    r"""Look directly inside ``root`` and a few known subfolders.

    REDkit installs keep the DB inside ``r4data\``; Witcher 2 keeps it inside
    ``bin\``. We also walk ``max_depth`` levels for shallow REDkit-project
    layouts, but skip noisy build/backup folders.
    """

    root = Path(root)
    if not root.is_dir():
        return []
    out = []
    # Direct hits — both file in root itself, and well-known subfolders that
    # are cheap to probe (no directory listing required).
    for name in db_names:
        candidate = root / name
        if candidate.is_file():
            out.append(candidate)
    for subdir in _DB_SUBDIRS:
        sub = root / subdir
        if sub.is_dir():
            for name in db_names:
                candidate = sub / name
                if candidate.is_file():
                    out.append(candidate)
    if out or max_depth <= 0:
        return out
    try:
        entries = sorted(root.iterdir())
    except Exception:
        entries = []
    for entry in entries:
        if not entry.is_dir():
            continue
        name_lower = entry.name.lower()
        if name_lower in {
            "backup", "backups", "build", "packed", ".git",
            "speech", "audio", "audio_original", "lipsync",
            "logs", "tmp", "cache",
        }:
            continue
        out.extend(_scan_for_db(entry, db_names, max_depth=max_depth - 1))
        if out:
            break
    return out


def _addon_prefs():
    """Return the addon's preferences regardless of legacy / extension install.

    Traditional installs register under the package name (``witcher3_tools``);
    extension installs register under the full dotted path (e.g.
    ``bl_ext.user_default.witcher3_tools``). Strip just the trailing
    ``.strings_browser`` so we end up with the addon root either way.
    """
    try:
        import bpy

        addons = bpy.context.preferences.addons
        own_package = __package__ or __name__
        addon_key = own_package.rsplit(".", 1)[0] if "." in own_package else own_package

        try:
            entry = addons.get(addon_key) if hasattr(addons, "get") else addons[addon_key]
        except Exception:
            entry = None
        if entry is None:
            # Fallback: full-path match for any addon whose key ends with our
            # parent module name. Avoids matching unrelated bl_ext addons.
            for key in getattr(addons, "keys", lambda: [])():
                key_str = str(key)
                if key_str == addon_key or key_str.endswith("." + addon_key.split(".")[-1]):
                    entry = addons[key]
                    break
        return getattr(entry, "preferences", None) if entry is not None else None
    except Exception:
        return None


def _redkit_project_paths():
    prefs = _addon_prefs()
    if prefs is None:
        return []
    paths = []
    try:
        import bpy

        for item in getattr(prefs, "redkit_projects", []) or []:
            path = str(getattr(item, "path", "") or "").strip()
            if not path:
                continue
            try:
                path = bpy.path.abspath(path)
            except Exception:
                pass
            paths.append(Path(os.path.normpath(path)))
    except Exception:
        pass
    return paths


def _w3_search_roots():
    roots = []
    prefs = _addon_prefs()
    if prefs is not None:
        for attr in ("witcher_game_path", "uncook_path"):
            val = str(getattr(prefs, attr, "") or "").strip()
            if val and os.path.isdir(val):
                roots.append(Path(val))
        depot = str(getattr(prefs, "redkit_depot_path", "") or "").strip()
        if depot and os.path.isdir(depot):
            roots.append(Path(depot))
    roots.extend(_redkit_project_paths())
    return roots


def _w2_search_roots():
    roots = []
    prefs = _addon_prefs()
    if prefs is not None:
        for attr in ("witcher2_game_path", "w2_unbundle_path"):
            val = str(getattr(prefs, attr, "") or "").strip()
            if val and os.path.isdir(val):
                roots.append(Path(val))
    # REDkit project entries are shared today — try them for W2 too.
    roots.extend(_redkit_project_paths())
    return roots


def find_string_db_paths(game, override_path=""):
    """Return candidate SQLite DB paths for ``game``.

    The override can be either a ``.db``/``.sqlite`` file or a directory. When a
    directory is given, we scan it (and its ``r4data``/``bin`` subfolders) for
    the known DB filenames. This lets users paste a REDkit install root like
    ``C:\\w3.modding\\The Witcher 3 REDkit`` and have both DBs picked up.
    """

    game_upper = str(game or "").upper()
    if game_upper == GAME_W3:
        db_names = W3_REDKIT_DB_NAMES
        roots = _w3_search_roots()
    elif game_upper == GAME_W2:
        db_names = W2_REDKIT_DB_NAMES
        roots = _w2_search_roots()
    else:
        return []

    override_path = str(override_path or "").strip()
    if override_path:
        try:
            override_p = Path(override_path)
        except Exception:
            override_p = None
        if override_p is not None:
            if override_p.is_file():
                return [override_p]
            if override_p.is_dir():
                hits = _scan_for_db(override_p, db_names)
                if hits:
                    return hits
                # If the override is a real folder but had no DB, still return
                # nothing rather than silently falling back so the UI surfaces
                # the misconfiguration.
                return []

    found = []
    seen = set()
    for root in roots:
        for candidate in _scan_for_db(root, db_names):
            key = str(candidate).lower()
            if key not in seen:
                seen.add(key)
                found.append(candidate)

    # Sort by file size descending so the actual populated DB always wins over
    # an empty overlay shell. A pristine REDkit ``_mod.db`` / ``user.sqlite``
    # is ~5-32 KB while the real ``LocalEditorStringDataBaseW3_UTF8.db`` /
    # ``base.sqlite`` is hundreds of megabytes — any size-based ranking puts
    # the right file first.
    def _size(path):
        try:
            return path.stat().st_size
        except Exception:
            return 0

    found.sort(key=_size, reverse=True)
    return found


# ---------------------------------------------------------------------------
# SQLite readers
# ---------------------------------------------------------------------------

_LANG_COLUMN_BY_HANDLE = {
    "en": "EN",
    "pl": "PL",
    "de": "DE",
    "fr": "FR",
    "it": "IT",
    "es": "ES",
    "esmx": "ESMX",
    "esMX": "ESMX",
    "cz": "CZ",
    "br": "BR",
    "ru": "RU",
    "ar": "AR",
    "tr": "TR",
    "cn": "CN",
    "zh": "ZH",
    "kr": "KR",
    "jp": "JP",
    "hu": "HU",
}

# LANG integer ids when the DB doesn't ship a LANGUAGES lookup table
# (Witcher 2's base.sqlite / user.sqlite). Mirrors LANGUAGES on the W3 REDkit
# DB and matches dialog_language._LANGUAGE_LOCALE_IDS.
_FALLBACK_LANG_ID_BY_HANDLE = {
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
    "esmx": 15,
    "cn": 16,
    "ar": 17,
    "debug": 20,
}


def _language_column(language):
    handle = str(language or "en").strip()
    return _LANG_COLUMN_BY_HANDLE.get(handle, _LANG_COLUMN_BY_HANDLE.get(handle.lower(), "EN"))


def _table_names(cursor):
    try:
        rows = cursor.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        ).fetchall()
        return {str(r[0]).upper() for r in rows}
    except Exception:
        return set()


def _utf8_lenient_text_factory(raw):
    """Decode SQLite TEXT bytes without crashing on stray bytes.

    Witcher 2's ``base.sqlite`` mixes encodings: some lines were imported from
    a Polish Windows code page and contain bytes that aren't valid UTF-8. The
    default ``text_factory`` in sqlite3 raises ``OperationalError`` on those
    rows, killing the entire query. Replacing it with a permissive UTF-8
    decoder (fallback to cp1252) keeps all rows readable.
    """
    if isinstance(raw, str):
        return raw
    try:
        return raw.decode("utf-8")
    except (UnicodeDecodeError, AttributeError):
        pass
    try:
        return raw.decode("cp1252")
    except (UnicodeDecodeError, AttributeError):
        pass
    return raw.decode("utf-8", errors="replace") if hasattr(raw, "decode") else str(raw)


def _connect_readonly(db_path):
    """Open ``db_path`` read-only across the SQLite versions Blender may ship.

    Tries two layers in order, picking whichever returns first:

    1. Properly-encoded ``Path.as_uri()`` with ``mode=ro``. Handles spaces and
       Windows drive letters safely on every Python 3.10+ build.
    2. Plain ``sqlite3.connect`` followed by ``PRAGMA query_only=ON``. Used as
       a fallback when URI parsing fails (some bundled SQLite builds are
       stricter than CPython's).

    Either way, a permissive ``text_factory`` is attached so non-UTF-8 byte
    sequences in the data don't crash the entire query.
    """
    db_str = str(db_path)
    conn = None
    try:
        uri = Path(db_str).as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    except Exception as exc:
        log.debug("URI connect failed for %s: %s", db_str, exc)
        conn = sqlite3.connect(db_str, timeout=2.0)
        try:
            conn.execute("PRAGMA query_only = ON")
        except Exception:
            log.debug("Failed to set query_only on %s", db_str, exc_info=True)

    conn.text_factory = _utf8_lenient_text_factory
    return conn


def _query_latest_strings(cursor, language_id):
    """Return [(string_id, text)] for the requested LANG id via LATEST_STRINGS."""
    try:
        rows = cursor.execute(
            "SELECT STRING_ID, TEXT FROM LATEST_STRINGS WHERE LANG = ?",
            (int(language_id),),
        ).fetchall()
    except Exception:
        return []
    out = []
    for sid, text in rows:
        try:
            out.append((int(sid), str(text or "")))
        except (TypeError, ValueError):
            continue
    return out


def _resolve_language_id(cursor, language, has_languages_table):
    """Find the LANG integer id for ``language``.

    Prefers the LANGUAGES table when it exists (W3 REDkit DB); falls back to a
    hard-coded mapping based on the documented REDkit locale ids when missing
    (W2 base.sqlite / user.sqlite).
    """
    column = _language_column(language).upper()
    if has_languages_table:
        try:
            rows = cursor.execute("SELECT ID, LANG FROM LANGUAGES").fetchall()
        except Exception:
            rows = []
        for lang_id, lang_name in rows:
            if str(lang_name or "").strip().upper() == column:
                try:
                    return int(lang_id)
                except (TypeError, ValueError):
                    continue
    # Fallback table — keyed on canonical language handle, not the DB column.
    handle = str(language or "en").strip().lower()
    return _FALLBACK_LANG_ID_BY_HANDLE.get(handle, _FALLBACK_LANG_ID_BY_HANDLE.get("en"))


def read_sqlite_strings(db_path, language="en", max_records=MAX_RECORDS_PER_SOURCE):
    """Load all STRING_INFO rows joined with the language-specific text.

    Returns: (list_of_tuple_rows, used_language_column).

    Each tuple is (string_id, string_key, voiceover, resource, property, text).

    Handles three known schema shapes:

      1. **W3 REDkit DB** — has LANGUAGES, LATEST_STRINGS view, STRING_INFO.
         Joins LATEST_STRINGS with STRING_INFO on (STRING_ID, LANG).
      2. **W2 DB (base.sqlite / user.sqlite)** — only STRINGS + STRING_INFO.
         Synthesises a latest-version subquery from STRINGS, using the fallback
         LANG-id map since LANGUAGES doesn't exist.
      3. **Anything else with a wide STRING_INFO that stores text in language
         columns** — queried directly.
    """

    db_path = Path(db_path)
    text_column = _language_column(language)
    if not db_path.is_file():
        _LAST_BUILD_ERROR["__last_sqlite_error__"] = f"File does not exist: {db_path}"
        return [], text_column

    rows_out = []
    cache_path = str(db_path)
    try:
        conn = _connect_readonly(db_path)
        try:
            cursor = conn.cursor()
            tables = _table_names(cursor)

            if "STRING_INFO" not in tables:
                log.info("DB %s lacks STRING_INFO; skipping", db_path)
                return [], text_column

            has_languages = "LANGUAGES" in tables
            has_latest_view = "LATEST_STRINGS" in tables
            has_strings_table = "STRINGS" in tables
            language_id = _resolve_language_id(cursor, language, has_languages)

            if has_latest_view and language_id is not None:
                # Best case: REDkit-built view already collapses VERSION.
                query = (
                    "SELECT si.STRING_ID, "
                    "       COALESCE(si.STRING_KEY,'') AS K, "
                    "       COALESCE(si.VOICEOVER_NAME,'') AS V, "
                    "       COALESCE(si.RESOURCE,'') AS R, "
                    "       COALESCE(si.PROPERTY_NAME,'') AS P, "
                    "       COALESCE(ls.TEXT,'') AS T "
                    "FROM STRING_INFO si "
                    "LEFT JOIN LATEST_STRINGS ls "
                    "  ON ls.STRING_ID = si.STRING_ID AND ls.LANG = ?"
                )
                cursor.execute(query, (int(language_id),))
            elif has_strings_table and language_id is not None:
                # W2 path: derive the newest VERSION per STRING_ID inside the
                # requested LANG with a correlated subquery, then LEFT JOIN
                # STRING_INFO for the metadata columns.
                query = (
                    "SELECT si.STRING_ID, "
                    "       COALESCE(si.STRING_KEY,'') AS K, "
                    "       COALESCE(si.VOICEOVER_NAME,'') AS V, "
                    "       COALESCE(si.RESOURCE,'') AS R, "
                    "       COALESCE(si.PROPERTY_NAME,'') AS P, "
                    "       COALESCE(s.TEXT,'') AS T "
                    "FROM STRING_INFO si "
                    "LEFT JOIN STRINGS s "
                    "  ON s.STRING_ID = si.STRING_ID AND s.LANG = ? "
                    "  AND s.VERSION = ( "
                    "    SELECT MAX(VERSION) FROM STRINGS s2 "
                    "    WHERE s2.STRING_ID = si.STRING_ID AND s2.LANG = ? "
                    "  )"
                )
                cursor.execute(query, (int(language_id), int(language_id)))
            else:
                # Wide-column STRING_INFO with TEXT in a language column.
                lang_col_quoted = f'"{text_column}"'
                query = (
                    f"SELECT STRING_ID, "
                    f"       COALESCE(STRING_KEY,''), "
                    f"       COALESCE(VOICEOVER_NAME,''), "
                    f"       COALESCE(RESOURCE,''), "
                    f"       COALESCE(PROPERTY_NAME,''), "
                    f"       COALESCE({lang_col_quoted},'') "
                    f"FROM STRING_INFO"
                )
                try:
                    cursor.execute(query)
                except Exception:
                    cursor.execute(
                        "SELECT STRING_ID, "
                        "       COALESCE(STRING_KEY,''), "
                        "       COALESCE(VOICEOVER_NAME,''), "
                        "       COALESCE(RESOURCE,''), "
                        "       COALESCE(PROPERTY_NAME,''), "
                        "       '' "
                        "FROM STRING_INFO"
                    )

            for row in cursor.fetchmany(max_records):
                rows_out.append(row)
        finally:
            conn.close()
    except Exception as exc:
        log.warning("Failed to read string DB %s", db_path, exc_info=True)
        # Store a short diagnostic the UI can pick up. We don't have the cache
        # key here so callers (build_sqlite_records / get_records) re-record
        # it under the proper key.
        _LAST_BUILD_ERROR["__last_sqlite_error__"] = f"{type(exc).__name__}: {exc}"
        return [], text_column

    return rows_out, text_column


# ---------------------------------------------------------------------------
# Record builders
# ---------------------------------------------------------------------------

def _w2_strings_search_roots(override_path=""):
    """Locate the W2 game install root(s) holding ``CookedPC/<lang>0.w2strings``.

    Mirrors the SQLite override behaviour: a file or folder override is honoured
    first; otherwise the configured W2 game/unbundle paths are scanned.
    """
    override_path = str(override_path or "").strip()
    roots = []
    if override_path:
        try:
            override_p = Path(override_path)
        except Exception:
            override_p = None
        if override_p is not None:
            if override_p.is_dir():
                roots.append(override_p)
            elif override_p.is_file():
                # File override: walk up looking for CookedPC. Useful when the
                # user picks ``en0.w2strings`` directly.
                for parent in override_p.parents:
                    if parent.name.lower() == "cookedpc" or (parent / "CookedPC").is_dir():
                        roots.append(parent if (parent / "CookedPC").is_dir() else parent.parent)
                        break
                else:
                    roots.append(override_p.parent)
    if not roots:
        roots = _w2_search_roots()
    return roots


def find_w2_strings_files(override_path=""):
    """Return ``Path`` instances for all discovered W2 .w2strings files."""
    from ..CR2W.witcher_cache.W2Strings import find_w2_strings_files as _walker

    return _walker(_w2_strings_search_roots(override_path))


def build_w2_binary_records(
    language="en",
    override_path="",
    max_records=MAX_RECORDS_PER_SOURCE,
):
    """Build StringRecord rows from the cached W2StringManager."""

    try:
        from ..CR2W.witcher_cache.W2Strings import (
            LoadWitcher2StringsManager,
            W2StringManager,
        )
    except Exception:
        log.warning("W2StringManager unavailable", exc_info=True)
        return []

    try:
        if str(override_path or "").strip():
            manager = W2StringManager()
            manager.Load(language, _w2_strings_search_roots(override_path))
        else:
            manager = LoadWitcher2StringsManager(language=language)
    except Exception:
        log.warning("Failed to load W2StringManager", exc_info=True)
        return []

    lines = getattr(manager, "Lines", None) or {}
    if not lines:
        return []

    id_to_key = getattr(manager, "IdToKey", {}) or {}
    source_by_id = getattr(manager, "SourcePathById", {}) or {}
    cache_files = getattr(manager, "cache_files", []) or [""]
    records = []
    for sid, text in lines.items():
        if len(records) >= max_records:
            break
        try:
            sid_int = int(sid)
        except (TypeError, ValueError):
            continue

        records.append(
            make_record(
                game=GAME_W2,
                source=SOURCE_W2STRINGS,
                string_id=sid_int,
                string_key=id_to_key.get(sid_int, ""),
                text=text or "",
                speaker=resolve_speaker(sid_int, ""),
                # W2 binary tables do not carry voiceover ids; W2 speech can
                # fill this later when the .w2speech source is wired in.
                speaker_code="",
                language=str(getattr(manager, "Language", language) or language),
                db_path=source_by_id.get(sid_int, cache_files[0]),
            )
        )

    return records


def build_w3_binary_records(language="en", max_records=MAX_RECORDS_PER_SOURCE):
    """Build StringRecord rows from the cached W3StringManager (binary cache)."""

    try:
        from ..CR2W.witcher_cache.W3Strings import LoadStringsManager
    except Exception:
        log.warning("W3StringManager unavailable", exc_info=True)
        return []

    try:
        manager = LoadStringsManager()
    except Exception:
        log.warning("Failed to load W3StringManager", exc_info=True)
        return []

    lines = getattr(manager, "Lines", None) or {}
    if not lines:
        return []

    records = []
    count = 0
    for sid, text in lines.items():
        if count >= max_records:
            break
        try:
            sid_int = int(sid)
        except (TypeError, ValueError):
            continue
        text_value = "" if text is None else str(text)
        # voice_names.json (loaded by resolve_speaker) supplies the raw 4-letter
        # code; we surface it on the record so the picker can show it next to
        # the resolved display name.
        voice_names = _load_voice_names_json()
        raw_code = voice_names.get(str(sid_int), "")
        records.append(
            make_record(
                game=GAME_W3,
                source=SOURCE_W3STRINGS,
                string_id=sid_int,
                text=text_value,
                speaker=resolve_speaker(sid_int, ""),
                speaker_code=raw_code,
                language=str(getattr(manager, "Language", language) or language),
            )
        )
        count += 1
    return records


def build_sqlite_records(
    game,
    db_path,
    language="en",
    max_records=MAX_RECORDS_PER_SOURCE,
):
    rows, lang_column = read_sqlite_strings(db_path, language=language, max_records=max_records)
    if not rows:
        return []

    records = []
    for row in rows:
        try:
            sid_int = int(row[0])
        except (TypeError, ValueError):
            continue
        string_key = row[1] if len(row) > 1 else ""
        voiceover = row[2] if len(row) > 2 else ""
        resource = row[3] if len(row) > 3 else ""
        property_name = row[4] if len(row) > 4 else ""
        text = row[5] if len(row) > 5 else ""
        speaker_code = _speaker_code_from_voiceover(voiceover, sid_int)
        if game == GAME_W3 and not speaker_code:
            speaker_code = _load_voice_names_json().get(str(sid_int), "")

        records.append(
            make_record(
                game=game,
                source=SOURCE_SQLITE,
                string_id=sid_int,
                string_key=string_key,
                text=text,
                voiceover=voiceover,
                speaker=resolve_speaker(sid_int, voiceover),
                speaker_code=speaker_code,
                resource=resource,
                property_name=property_name,
                language=lang_column.lower(),
                db_path=str(db_path),
            )
        )
    return records


# ---------------------------------------------------------------------------
# Build orchestrator + in-memory cache
# ---------------------------------------------------------------------------

# Cache key: (game, source, db_path_or_'', language). Each value is a list of
# StringRecord dicts.
_RECORDS_CACHE = {}
_RECORDS_BUILT_AT = {}
_CACHE_TTL_SECONDS = 300.0

# Per-cache-key diagnostic: last error message produced while building (or "").
# Surfaced by the UI so the user can see *why* a source returned 0 rows.
_LAST_BUILD_ERROR = {}


def cache_clear():
    _RECORDS_CACHE.clear()
    _RECORDS_BUILT_AT.clear()
    _LAST_BUILD_ERROR.clear()
    clear_speaker_cache()


def get_last_error(game, source, db_path="", language="en"):
    """Return the diagnostic captured during the most recent build, or ''."""
    return _LAST_BUILD_ERROR.get(_cache_key(game, source, db_path, language), "")


def _cache_key(game, source, db_path, language):
    return (
        str(game or "").upper(),
        str(source or "").lower(),
        str(db_path or ""),
        str(language or "en").lower(),
    )


def get_records(game, source, *, language="en", db_path="", force_reload=False):
    """Return the StringRecord list for the requested source, building once.

    Non-empty build results are cached in memory for ``_CACHE_TTL_SECONDS``
    seconds. **Empty results are NEVER cached** — they almost always mean
    something transient failed (DB locked, path not yet typed, mid-fix module
    state) and serving them back from cache stops the user from recovering by
    just retrying. A forced reload bypasses the cache unconditionally.
    """

    key = _cache_key(game, source, db_path, language)
    now = time.time()
    if not force_reload:
        cached = _RECORDS_CACHE.get(key)
        if cached:  # non-empty list only — empty results re-run the build
            built_at = _RECORDS_BUILT_AT.get(key, 0.0)
            if now - built_at < _CACHE_TTL_SECONDS:
                return cached

    # Clear stale per-call sqlite error before the build attempt.
    _LAST_BUILD_ERROR.pop("__last_sqlite_error__", None)

    game_upper = str(game or "").upper()
    source_lower = str(source or "").lower()
    error_message = ""
    log.info(
        "strings_browser: building records for game=%s source=%s db_path=%r language=%s",
        game_upper, source_lower, db_path, language,
    )
    try:
        if source_lower == SOURCE_W3STRINGS:
            records = build_w3_binary_records(language=language)
        elif source_lower == SOURCE_W2STRINGS:
            # db_path doubles as an override here so the same cache key shape
            # works for both binary and SQLite sources.
            records = build_w2_binary_records(language=language, override_path=db_path)
        elif source_lower == SOURCE_SQLITE:
            if not db_path:
                records = []
                error_message = "No database path set (auto-detect found nothing; use the override field)."
            else:
                records = build_sqlite_records(game_upper, db_path, language=language)
        else:
            records = []
    except Exception as exc:
        log.warning("get_records build failed for %s/%s", game_upper, source_lower, exc_info=True)
        records = []
        error_message = f"{type(exc).__name__}: {exc}"

    # Fold in any sqlite-layer error captured during build.
    sqlite_error = _LAST_BUILD_ERROR.pop("__last_sqlite_error__", "")
    if sqlite_error and not error_message:
        error_message = sqlite_error

    log.info(
        "strings_browser: built %d records for game=%s source=%s (error=%r)",
        len(records), game_upper, source_lower, error_message,
    )

    if records:
        _RECORDS_CACHE[key] = records
        _RECORDS_BUILT_AT[key] = now
    else:
        # Drop any previously-cached entry under this key so the next refresh
        # actually retries rather than re-serving a stale empty list.
        _RECORDS_CACHE.pop(key, None)
        _RECORDS_BUILT_AT.pop(key, None)
    _LAST_BUILD_ERROR[key] = error_message
    return records


def collect_speakers(records, *, top=200):
    """Return [(speaker_upper, count)] sorted by descending count."""
    counts = {}
    for rec in records:
        speaker = rec.get("speaker") or ""
        if not speaker:
            continue
        counts[speaker] = counts.get(speaker, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:top]


def filter_records(records, *, search_text="", speaker_filter=""):
    """Apply substring search + speaker equality. Returns filtered list."""

    search_text = str(search_text or "").strip().lower()
    speaker_filter = str(speaker_filter or "").strip().upper()
    if not search_text and not speaker_filter:
        return list(records)

    terms = [token for token in search_text.split() if token] if search_text else []

    out = []
    for rec in records:
        if speaker_filter and rec.get("speaker") != speaker_filter:
            continue
        if terms:
            blob = rec.get("search_blob", "")
            if not all(term in blob for term in terms):
                continue
        out.append(rec)
    return out
