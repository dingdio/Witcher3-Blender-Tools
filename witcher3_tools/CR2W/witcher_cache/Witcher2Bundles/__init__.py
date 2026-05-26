from .DzipArchive import DzipArchive, DzipItem, InvalidDzipException
from .DzipManager import DzipManager


def LoadWitcher2BundleManager(reset_cache=False, game_path: str = ""):
    return DzipManager.Get(reset_cache=reset_cache, game_path=game_path)
