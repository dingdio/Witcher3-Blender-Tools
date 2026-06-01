from .SpeechManager import SpeechManager
def LoadSpeechManager(language=None, do_reload=False):
    try:
        return SpeechManager.Get(do_reload=do_reload, language=language)
    except Exception as e:
        raise e
