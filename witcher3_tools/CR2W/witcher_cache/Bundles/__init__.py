from .BundleManager import BundleManager
def LoadBundleManager(loadmods=False, reset_cache = False):
    try:
        return BundleManager.Get(loadmods, reset_cache)
    except Exception as e:
        raise e