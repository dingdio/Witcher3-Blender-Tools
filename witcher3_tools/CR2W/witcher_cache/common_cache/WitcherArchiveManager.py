from abc import ABC, abstractmethod
from enum import Enum
from ..blender_common import get_game_path
import os

class Configuration:
    ExecutablePath = get_game_path()
    GameModDir = os.path.join(ExecutablePath, "mods")
    GameDlcDir = os.path.join(ExecutablePath, "dlc")

class EBundleType(Enum):
    ANY = 1
    BUNDLE = 2
    COLLISIONCACHE = 3
    TEXTURECACHE = 4
    SOUNDCACHE = 5
    SPEECH = 6
    SHADER = 7

class WitcherArchiveManager(ABC):
    VanillaDLClist = ["dlc1", "dlc2", "dlc3", "dlc4", "dlc5", "dlc6", "dlc7", "dlc8", "dlc9", "dlc10", "dlc11", "dlc12", "dlc13", "dlc14", "dlc15", "dlc16", "dlc17", "dlc18", "dlc20", "bob", "ep1"]
       
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