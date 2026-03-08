from .SpeechManager import SpeechManager
def LoadSpeechManager():
    try:
        return SpeechManager.Get()
    except Exception as e:
        raise e