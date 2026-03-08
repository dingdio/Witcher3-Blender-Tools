
from .Bundles import LoadBundleManager
from .TextureCache.texture_manager import TextureManager
from .Bundles.BundleManager import BundleManager
from .common_cache.WitcherArchiveManager import Configuration

class CacheController:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            # Assuming Configuration and LoggerService are defined elsewhere
            # Initialize your Configuration and Logger here as needed
            cls._instance._BundleManager = LoadBundleManager() #"CAKETOWN"
        return cls._instance

    def __init__(self):
        pass
        # self._W3StringManager = None
        # self._BundleManager = None
        # self._ModBundleManager = None
        # self._SoundManager = None
        # self._ModSoundManager = None
        # self._TextureManager = None
        # self._ModTextureManager = None
        # self._CollisionManager = None
        # self._ModCollisionManager = None
        # self._SpeechManager = None

    # @property
    # def W3StringManager(self):
    #     return self._W3StringManager
    
    # @W3StringManager.setter
    # def W3StringManager(self, value):
    #     self._W3StringManager = value

    @property
    def SoundManager(self):
        return self._SoundManager
    
    @SoundManager.setter
    def SoundManager(self, value):
        self._SoundManager = value

    @property
    def TextureManager(self):
        return self._TextureManager
    
    @TextureManager.setter
    def TextureManager(self, value):
        self._TextureManager = value

    def GetManagers(self, loadmods = False):
        managers = []
        # Example Configuration and MainController usage, these need to be defined in your Python code
        # exeDir = os.path.dirname(Configuration.ExecutablePath)

        if loadmods:
            self._ModBundleManager = BundleManager().Get(True, True)  # Assuming BundleManager is defined elsewhere
            self._ModBundleManager.LoadModsBundles(Configuration.GameModDir, Configuration.GameDlcDir)
            managers.append(self._ModBundleManager)

            # self._ModTextureManager = TextureManager()  # Assuming TextureManager is defined elsewhere
            # self._ModTextureManager.LoadModsBundles(Configuration.GameModDir, Configuration.GameDlcDir)
            # managers.append(self._ModTextureManager)

            # self._ModSoundManager = SoundManager()  # Assuming SoundManager is defined elsewhere
            # self._ModSoundManager.LoadModsBundles(Configuration.GameModDir, Configuration.GameDlcDir)
            # managers.append(self._ModSoundManager)

            # self._ModCollisionManager = CollisionManager()  # Assuming CollisionManager is defined elsewhere
            # self._ModCollisionManager.LoadModsBundles(Configuration.GameModDir, Configuration.GameDlcDir)
            # managers.append(self._ModCollisionManager)
        else:
            if self._BundleManager is not None:
                managers.append(self._BundleManager)
            # if self._SoundManager is not None:
            #     managers.append(self._SoundManager)
            # if self._TextureManager is not None:
            #     managers.append(self._TextureManager)
            # if self._CollisionManager is not None:
            #     managers.append(self._CollisionManager)
            # if self._SpeechManager is not None:
            #     managers.append(self._SpeechManager)

        return managers
    