"""Compatibility wrapper for the W2 strings cache implementation."""

from ..CR2W.witcher_cache.W2Strings.W2Language import (
    W2_FILENAME_LANGUAGE_PREFIXES,
    W2_MAGIC_BY_FILE_KEY,
    language_handle_from_filename,
    normalize_language_handle,
)
from ..CR2W.witcher_cache.W2Strings.W2StringFile import (
    W2StringFile,
    W2StringsFile,
    W2StringsParseError,
    find_w2_strings_files,
)


__all__ = (
    "W2_FILENAME_LANGUAGE_PREFIXES",
    "W2_MAGIC_BY_FILE_KEY",
    "W2StringFile",
    "W2StringsFile",
    "W2StringsParseError",
    "find_w2_strings_files",
    "language_handle_from_filename",
    "normalize_language_handle",
)
