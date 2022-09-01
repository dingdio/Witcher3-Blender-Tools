
from typing import List
from .witcher_classes import CName


class CModStoryBoardActor(object):
    """docstr for CModStoryBoardActor."""
    def __init__(self):
        super(CModStoryBoardActor, self).__init__()
        self.templatePath: str = "characters\npc_entities\main_npc\avallach.w2ent"
        # ------------------------------------------------------------------------
        # determines a *compatible* idle animation to use if no pose was selected
        # (yet/removed). has to be probed on adjusted on every template change!
        self._defaultIdleAnim: CName =  'high_standing_determined_idle'
        # ------------------------------------------------------------------------
        # special doppler template for cloning player entity
        self._playerCloneTemplate: str
        # NOTE: DO NOT CHANGE! this must be exactly as the entry in the template csv!
        self.playerCloneTemplate = "dlc\modtemplates\storyboardui\geralt_npc.w2ent"
        # ------------------------------------------------------------------------
        self._appearanceNames: List[CName]
        self._appearanceId: int
        #_mimicsTriggerScene: CStoryScene

        # current look at state
        self._isStaticLookAt: bool
        self._isActiveLookAt: bool

        # coarse classification for prefiltering animations
        #self._cachedActorType: EStoryBoardActorType = ESB_AT_Untested
        
        self._animPaths: List[str] = []
        
    def isCompatibleAnimation(self, animPath:str): #animId:CName):
        #GetComponentByClassName('CAnimatedComponent')
        if animPath in self._animPaths:
            return True
        return False

    