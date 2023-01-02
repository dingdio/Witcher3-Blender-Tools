# -----------------------------------------------------------------------------
#
# BUGS:
#
# TODO:
#  - adjust animation list with flags: isHuman, isMonster, isMan, isWoman,
#      isAnimal and use the information to prefilter (without triggering animations)
#
# ----------------------------------------------------------------------------
# ----------------------------------------------------------------------------

import csv
import os
from pathlib import Path
from typing import List, Tuple, Dict, cast
from dataclasses import dataclass

from numpy import append

from .mod_additional_animations import SBUI_getExtraAnimations, SSbUiExtraAnimation

from .filtered_list import CModUiFilteredList, SModUiCategorizedListItem
from .storyboardasset import CModStoryBoardActor
from .witcher_classes import C2dArray, CName


@dataclass
class SStoryBoardAnimationInfo:
    path: str
    cat1: str
    cat2: str
    cat3: str
    id: CName
    caption: str
    frames: int
    slotId: int
    
def LoadCSV(path):
    reader = csv.DictReader(open(path), delimiter=";")
    return reader
# ----------------------------------------------------------------------------


    
# Wrapper class so list can be passed by reference
class CStoryBoardAnimationMetaInfo():
    # contains info about all animations. the slot number for an animation will
    # be used as id in the filtered UI listview. this is required as the UI
    # returns the selected option id as str and there is no str -> name
    # conversion available but playing animations requires the anim name as CName.
    # meaning: this array is also used as ui selected anim id -> cname anim id LUT

    def __init__(self):
        self.animList: List[SStoryBoardAnimationInfo] = []
    # ------------------------------------------------------------------------
    def loadCsv(self, path: str):
        data: C2dArray
        i: int
        data = LoadCSV(path)
        # csv: path;CAT1;CAT2;CAT3;id;caption;frames
        for row in data:
            row = list(row.values())
            self.animList.append(SStoryBoardAnimationInfo(
                row[0],
                row[1],
                row[2],
                row[3],
                row[4],
                row[5],
                int(row[6]),
                # since extra animations are added as top category the slot
                # position in the animList array does not match the numerical
                # part of the repo animid anymore
                # therefore explicitely store the slot from the vanilla data csv
                # to be used for id generation
                #i + 1,
                data.line_num-1,
            ))

    # ------------------------------------------------------------------------
    def addExtraAnimations(self, extraAnims: List[SSbUiExtraAnimation]) -> int:
        topCat: str
        i: int

        self.animList.clear()
        # provide entry for "empty" (aka no) animation as first entry
        self.animList.append(SStoryBoardAnimationInfo("","","","",'no animation', "-no animation-", 0, 0))

        topCat = "SBUI_ExtraAnimCat" #GetLocStringByKeyExt("SBUI_ExtraAnimCat")

        for i in range(0, len(extraAnims)): # for (i = 0; i < extraAnims.Size(); i += 1) {
            pass
            self.animList.append(SStoryBoardAnimationInfo(
                "customAnimPath",
                topCat,
                extraAnims[i].subCategory1,
                extraAnims[i].subCategory2,
                extraAnims[i].animName,
                extraAnims[i].caption,
                extraAnims[i].frames,
                # custom animids always start from 100000 so they do not collide
                # with vanilla repo ids (which use up to ~15K slots)
                100000 + extraAnims[i].animId,
            ))
        # number of extra animation (without empty slot)
        return len(self.animList) - 1
    # ------------------------------------------------------------------------

class CModSbUiAnimationList(CModUiFilteredList):
    # ------------------------------------------------------------------------
    def createCompatibleList(
        self, actor: CModStoryBoardActor, animInfo: CStoryBoardAnimationMetaInfo) -> int:

        mimicsMeta: CStoryBoardMimicsMetaInfo = False #cast(CStoryBoardMimicsMetaInfo, animInfo)

        #self._items.Clear()

        # first entry of animation lists is defined as no anim (id == 0)!
        self._items.append(SModUiCategorizedListItem(
            0,
            animInfo.animList[0].caption,
            animInfo.animList[0].cat1,
            animInfo.animList[0].cat2,
            animInfo.animList[0].cat3,
            False
        ))

        if (mimicsMeta):
            self.filterMimicsAnimations(actor, mimicsMeta)
        else:
            self.filterNormalAnimations(actor, animInfo)

        # anim compatibility probing plays animations -> last animation will
        # play to the end. looks strange -> prevent this
        #actor.resetCompatibilityCheckAnimations()

        return len(self._items)
    # ------------------------------------------------------------------------
    def filterNormalAnimations(
        self,
        actor: CModStoryBoardActor, animInfo: CStoryBoardAnimationMetaInfo):
        i: int
        # create a compatible list of animations by actor
        for i in range(0, len(animInfo.animList)):
        #for (i = 1; i < animInfo.animList.Size(); i += 1) {
            if (actor.isCompatibleAnimation(animInfo.animList[i].path)):
                self._items.append(SModUiCategorizedListItem(
                    # use numerical id (0 is defined as no anim!)
                    animInfo.animList[i].slotId,
                    animInfo.animList[i].caption,
                    animInfo.animList[i].cat1,
                    animInfo.animList[i].cat2,
                    animInfo.animList[i].cat3,
                    False
                ))
    # ------------------------------------------------------------------------
    # def filterMimicsAnimations(
    #     actor: CModStoryBoardActor, animInfo: CStoryBoardMimicsMetaInfo):
    #     i: int
    #     # create a compatible list of *mimics* animations by actor
    #     for (i = 1; i <= animInfo.animList.Size(); i += 1) {

    #         if (actor.isCompatibleMimicsAnimation(animInfo.animList[i].id)) {
    #             items.PushBack(SModUiCategorizedListItem(
    #                 animInfo.animList[i].slotId,
    #                 animInfo.animList[i].caption,
    #                 animInfo.animList[i].cat1,
    #                 animInfo.animList[i].cat2,
    #                 animInfo.animList[i].cat3,
    #             ))
    #         }
    #     }
    # }
    # ------------------------------------------------------------------------

# ----------------------------------------------------------------------------

# ----------------------------------------------------------------------------
class CStoryBoardMimicsMetaInfo(CStoryBoardAnimationMetaInfo):
    """docstring for CStoryBoardMimicsMetaInfo."""
    def __init__(self):
        super(CStoryBoardMimicsMetaInfo, self).__init__()

# ----------------------------------------------------------------------------
# Management of animations for actor assets per storyboard shot.
#  - selecting animation from available (actor compatible) list of animations
#

class CModStoryBoardAnimationListsManager(object):
    active = None # type: CModStoryBoardAnimationListsManager
    active_list = None # type: CModStoryBoardAnimationListsManager
    # ------------------------------------------------------------------------
    def __init__(self):
        super(CModStoryBoardAnimationListsManager, self).__init__()
        # ------------------------------------------------------------------------
        self._compatibleAnimationCount: int
        self._dataLoaded: bool
        self._extraAnimCount: int
        # ------------------------------------------------------------------------
        # contains info about all animations. the slot number for an animation will
        # be used as id in the filtered UI listview. this is required as the UI
        # returns the selected option id as str and there is no str -> name
        # conversion available but playing animations requires the anim name as CName.
        # meaning: this array is also used as ui selected anim id -> cname anim id LUT
        self._animMeta: CStoryBoardAnimationMetaInfo
        CModStoryBoardAnimationListsManager.active = self

    def init(self):
        pass
    # ------------------------------------------------------------------------
    def lazyLoad(self):
        self._animMeta = CStoryBoardAnimationMetaInfo()
        self._extraAnimCount = self._animMeta.addExtraAnimations(SBUI_getExtraAnimations())
        RES_DIR = Path(__file__)
        RES_DIR = str(Path(RES_DIR).parents[1])
        self._animMeta.loadCsv(os.path.join(RES_DIR, "CR2W\\data\\actor_animations.csv"))
        self._dataLoaded = True
    # ------------------------------------------------------------------------
    def activate():
        pass
    # ------------------------------------------------------------------------
    def deactivate():
        pass
    # ------------------------------------------------------------------------
    def getAnimationListFor(self, actor: CModStoryBoardActor) -> CModSbUiAnimationList :
        actorAnims: CModSbUiAnimationList
        i: int

        if (not self._dataLoaded):
            self.lazyLoad()

        actorAnims = CModSbUiAnimationList()
        self._compatibleAnimationCount = actorAnims.createCompatibleList(actor, self._animMeta)
        
        CModStoryBoardAnimationListsManager.active_list = actorAnims
        return actorAnims
    # ------------------------------------------------------------------------
    def getAnimationCount(self)-> int:
        return self.compatibleAnimationCount
    # ------------------------------------------------------------------------
    def getAnimationName(self, selectedUiId: int) -> CName :
        i: int
        s: int
        selectedUiId = int(selectedUiId)

        if (not self._dataLoaded):
            self.lazyLoad()

        if (selectedUiId >= 100000):
            s = len(self._animMeta.animList)
            for i in range(0, s):
                if (self._animMeta.animList[i].slotId == selectedUiId):
                    return self._animMeta.animList[i].id
        return self._animMeta.animList[self._extraAnimCount + selectedUiId].id, self._animMeta.animList[self._extraAnimCount + selectedUiId].path
    # ------------------------------------------------------------------------
    def getAnimationFrameCount(self, selectedUiId: int) -> int :
        i: int
        s: int

        if (not self._dataLoaded):
            self.lazyLoad()

        if (selectedUiId >= 100000):
            s = len(self._animMeta.animList)
            for i in range(0, s):
                if (self._animMeta.animList[i].slotId == selectedUiId):
                    return self._animMeta.animList[i].frames
        return self._animMeta.animList[self._extraAnimCount + selectedUiId].frames
    # ------------------------------------------------------------------------

# ----------------------------------------------------------------------------



class CModStoryBoardMimicsListsManager(CModStoryBoardAnimationListsManager):
    """docstring for CModStoryBoardMimicsListsManager."""
    def __init__(self):
        super(CModStoryBoardMimicsListsManager, self).__init__()
    # ------------------------------------------------------------------------
    def lazyLoad(self):
        animMeta = CStoryBoardMimicsMetaInfo()
        extraAnimCount = 0 #animMeta.addExtraAnimations(SBUI_getExtraMimics())
        
        RES_DIR = Path(__file__)
        RES_DIR = str(Path(RES_DIR).parents[1])
        animMeta.loadCsv(os.path.join(RES_DIR, "CR2W\\data\\actor_mimics.csv"))
        dataLoaded = True
    # ------------------------------------------------------------------------
# ----------------------------------------------------------------------------
