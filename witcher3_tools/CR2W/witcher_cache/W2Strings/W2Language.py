from __future__ import annotations

import os


class W2LanguageKey:
    def __init__(self, value: int):
        self.value = int(value) & 0xFFFFFFFF

    def __str__(self):
        return str(self.value)


class W2LanguageMagic:
    def __init__(self, value: int):
        self.value = int(value) & 0xFFFFFFFF

    def __str__(self):
        return str(self.value)


class W2Language:
    def __init__(self, key: W2LanguageKey, magic: W2LanguageMagic, handle: str):
        self.Key = key
        self.Magic = magic
        self.Handle = str(handle or "").lower()

    def __str__(self):
        return f"W2Language({self.Key},{self.Magic},{self.Handle})"


languages = [
    W2Language(W2LanguageKey(0x83496237), W2LanguageMagic(0x73946816), "pl"),
    W2Language(W2LanguageKey(0x43975139), W2LanguageMagic(0x79321793), "en"),
    W2Language(W2LanguageKey(0x75886138), W2LanguageMagic(0x42791159), "de"),
    W2Language(W2LanguageKey(0x45931894), W2LanguageMagic(0x12375973), "it"),
    W2Language(W2LanguageKey(0x23863176), W2LanguageMagic(0x75921975), "fr"),
    W2Language(W2LanguageKey(0x24987354), W2LanguageMagic(0x21793217), "cz"),
    W2Language(W2LanguageKey(0x18796651), W2LanguageMagic(0x42387566), "es"),
    W2Language(W2LanguageKey(0x18632176), W2LanguageMagic(0x16875467), "zh"),
    W2Language(W2LanguageKey(0x63481486), W2LanguageMagic(0x42386347), "ru"),
    W2Language(W2LanguageKey(0x77932179), W2LanguageMagic(0x54932186), "ru"),
    W2Language(W2LanguageKey(0x42378932), W2LanguageMagic(0x67823218), "hu"),
    W2Language(W2LanguageKey(0x54834893), W2LanguageMagic(0x59825646), "jp"),
]


W2_FILENAME_LANGUAGE_PREFIXES = {
    "en": "en",
    "pl": "pl",
    "de": "de",
    "fr": "fr",
    "it": "it",
    "es": "es",
    "br": "br",
    "cz": "cz",
    "hu": "hu",
    "ru": "ru",
    "jp": "jp",
    "kr": "kr",
    "tr": "tr",
    "zh": "zh",
}


W2_MAGIC_BY_FILE_KEY = {lang.Key.value: lang.Magic.value for lang in languages}


def normalize_language_handle(language: str) -> str:
    handle = str(language or "en").strip().lower() or "en"
    return W2_FILENAME_LANGUAGE_PREFIXES.get(handle, handle)


def language_handle_from_filename(filename) -> str:
    base = os.path.basename(str(filename or "")).lower()
    stem, _ext = os.path.splitext(base)
    if stem.endswith("0"):
        stem = stem[:-1]
    return normalize_language_handle(stem)


def language_from_key(file_key: int, filename_handle: str = "") -> W2Language:
    file_key = int(file_key or 0) & 0xFFFFFFFF
    filename_handle = normalize_language_handle(filename_handle)
    for lang in languages:
        if lang.Key.value == file_key:
            return lang
    return W2Language(W2LanguageKey(file_key), W2LanguageMagic(0), filename_handle or "unknown")
