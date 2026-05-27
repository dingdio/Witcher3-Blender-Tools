from .W2Language import (
    W2Language,
    W2LanguageKey,
    W2LanguageMagic,
    W2_MAGIC_BY_FILE_KEY,
    language_handle_from_filename,
    normalize_language_handle,
)
from .W2StringFile import (
    W2StringFile,
    W2StringsFile,
    W2StringsParseError,
    find_w2_strings_files,
)
from .W2StringManager import (
    W2StringManager,
    current_witcher2_string_roots,
    find_w2_strings_files_for_path,
)


def LoadWitcher2StringsManager(do_reload=False, language=None, game_path: str = "", roots=None):
    return W2StringManager.Get(do_reload=do_reload, language=language, game_path=game_path, roots=roots)


def LoadStringsManager(do_reload=False, language=None, game_path: str = "", roots=None):
    return LoadWitcher2StringsManager(
        do_reload=do_reload,
        language=language,
        game_path=game_path,
        roots=roots,
    )
