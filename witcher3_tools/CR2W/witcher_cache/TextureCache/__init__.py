from .texture_manager import TextureManager
def LoadTextureManager(do_reload=False, loadmods=False):
    try:
        return TextureManager.Get(do_reload=do_reload, loadmods=loadmods)
    except Exception as e:
        raise e
