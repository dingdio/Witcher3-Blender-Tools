from .SpeechManager import SpeechManager
def LoadSpeechManager(language=None):
    try:
        return SpeechManager.Get(language=language)
    except Exception as e:
        raise e
