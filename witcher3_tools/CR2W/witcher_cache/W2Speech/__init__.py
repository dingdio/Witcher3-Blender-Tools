from .W2Speech import (
    W2Speech,
    W2SpeechEntry,
    W2SpeechParseError,
    w2_voice_base_name,
)
from .W2SpeechManager import (
    W2SpeechManager,
    current_witcher2_speech_roots,
    find_w2_speech_files,
    find_w2_speech_files_for_path,
)


def LoadWitcher2SpeechManager(do_reload=False, language=None, game_path: str = "", roots=None):
    return W2SpeechManager.Get(do_reload=do_reload, language=language, game_path=game_path, roots=roots)


def LoadSpeechManager(do_reload=False, language=None, game_path: str = "", roots=None):
    return LoadWitcher2SpeechManager(
        do_reload=do_reload,
        language=language,
        game_path=game_path,
        roots=roots,
    )
