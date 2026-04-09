from abc import ABC, abstractmethod
from enum import Enum
from ..blender_common import get_game_path
import os


class Configuration:
    ExecutablePath = get_game_path()
    GameModDir = os.path.join(ExecutablePath, "mods")
    GameDlcDir = os.path.join(ExecutablePath, "dlc")


def normalize_game_path(path: str) -> str:
    if not path:
        return ""
    try:
        return os.path.normpath(os.path.abspath(path))
    except Exception:
        return os.path.normpath(path)


def refresh_game_configuration_path() -> str:
    base_path = normalize_game_path(get_game_path())
    Configuration.ExecutablePath = base_path
    Configuration.GameModDir = os.path.join(base_path, "mods") if base_path else ""
    Configuration.GameDlcDir = os.path.join(base_path, "dlc") if base_path else ""
    return base_path


def has_game_content_root(base_path: str) -> bool:
    if not base_path:
        return False
    return os.path.isdir(os.path.join(base_path, "content"))


def has_game_content_or_dlc_root(base_path: str) -> bool:
    if not base_path:
        return False
    return has_game_content_root(base_path) or os.path.isdir(os.path.join(base_path, "dlc"))

class EBundleType(Enum):
    ANY = 1
    BUNDLE = 2
    COLLISIONCACHE = 3
    TEXTURECACHE = 4
    SOUNDCACHE = 5
    SPEECH = 6
    SHADER = 7


class WitcherArchiveManager(ABC):
    VANILLA_DLC_LIST = [
        "dlc1", "dlc2", "dlc3", "dlc4", "dlc5", "dlc6", "dlc7", "dlc8",
        "dlc9", "dlc10", "dlc11", "dlc12", "dlc13", "dlc14", "dlc15",
        "dlc16", "dlc17", "dlc18", "dlc20", "bob", "ep1",
    ]
    VanillaDLClist = VANILLA_DLC_LIST
       
    def __init__(self):
        pass
    
    @property
    @abstractmethod
    def TypeName(self):
        pass

    @abstractmethod
    def LoadModBundle(self, filename):
        pass

    @abstractmethod
    def LoadBundle(self, filename, ispatch=False):
        pass

    @abstractmethod
    def LoadAll(self, exedir):
        pass

    @abstractmethod
    def LoadModsBundles(self, mods, dlc):
        pass
    
    @staticmethod
    def GetModFolder(path):
        parts = path.split('\\')
        if len(parts) > 3 and "content" in parts:
            return parts[parts.index("content") - 1]
        return path
