from .SoundManager import SoundManager


def LoadSoundManager(do_reload=False, loadmods=False):
    try:
        return SoundManager.Get(do_reload=do_reload, loadmods=loadmods)
    except Exception as exc:
        raise exc
