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

# CSV filename per source game (relative to witcher3_tools/CR2W/data/).
_ACTOR_ANIM_CSV_BY_SOURCE = {
    "w3": "actor_animations.csv",
    "w2": "witcher_2_actor_animations.csv",
}


def _normalize_source_game(value) -> str:
    text = str(value or "").strip().lower().replace(" ", "")
    return "w2" if text in {"w2", "witcher2", "tw2"} else "w3"


class CModStoryBoardAnimationListsManager(object):
    active = None # type: CModStoryBoardAnimationListsManager
    active_list = None # type: CModStoryBoardAnimationListsManager
    # Per-source-game caches. Each CSV is loaded once and shared across
    # subsequent manager instances; the dropdown / armature can switch between
    # them without re-parsing.
    _shared_anim_meta_by_source: dict = {}
    _shared_extra_anim_count_by_source: dict = {}

    @classmethod
    def clear_shared_cache(cls):
        """Force every CSV to be re-read on the next lazyLoad() call."""
        cls._shared_anim_meta_by_source = {}
        cls._shared_extra_anim_count_by_source = {}
    # ------------------------------------------------------------------------
    def __init__(self):
        super(CModStoryBoardAnimationListsManager, self).__init__()
        # ------------------------------------------------------------------------
        self._compatibleAnimationCount: int
        self._dataLoaded: bool
        self._extraAnimCount: int
        self._loadedSourceGame: str = ""
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
    def lazyLoad(self, source_game: str = "w3"):
        source_game = _normalize_source_game(source_game)
        cls = CModStoryBoardAnimationListsManager
        cached_meta = cls._shared_anim_meta_by_source.get(source_game)
        if cached_meta is not None:
            self._animMeta = cached_meta
            self._extraAnimCount = cls._shared_extra_anim_count_by_source.get(source_game, 0)
            self._loadedSourceGame = source_game
            self._dataLoaded = True
            return
        meta = CStoryBoardAnimationMetaInfo()
        # Extra/custom animations are W3-only (the SBUI extras catalog is W3
        # depot-pathed). For W2 we still need the sentinel "-no animation-" entry
        # at animList[0] that createCompatibleList() uses as the placeholder.
        if source_game == "w3":
            extra_count = meta.addExtraAnimations(SBUI_getExtraAnimations())
        else:
            meta.animList.clear()
            meta.animList.append(SStoryBoardAnimationInfo("", "", "", "", "no animation", "-no animation-", 0, 0))
            extra_count = 0
        csv_name = _ACTOR_ANIM_CSV_BY_SOURCE.get(source_game, _ACTOR_ANIM_CSV_BY_SOURCE["w3"])
        RES_DIR = str(Path(__file__).parents[1])
        meta.loadCsv(os.path.join(RES_DIR, "CR2W", "data", csv_name))
        self._animMeta = meta
        self._extraAnimCount = extra_count
        self._loadedSourceGame = source_game
        cls._shared_anim_meta_by_source[source_game] = meta
        cls._shared_extra_anim_count_by_source[source_game] = extra_count
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

        source_game = _normalize_source_game(getattr(actor, "source_game", "") or "w3")
        if not self._dataLoaded or self._loadedSourceGame != source_game:
            self.lazyLoad(source_game)

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
    _shared_mimics_meta = None  # CSV loaded once, shared across all instances

    @classmethod
    def get_mimics_meta(cls) -> CStoryBoardAnimationMetaInfo:
        if cls._shared_mimics_meta is None:
            meta = CStoryBoardMimicsMetaInfo()
            RES_DIR = str(Path(Path(__file__).parents[1]))
            meta.loadCsv(os.path.join(RES_DIR, "CR2W\\data\\actor_mimics.csv"))
            cls._shared_mimics_meta = meta
        return cls._shared_mimics_meta

    def __init__(self):
        super(CModStoryBoardMimicsListsManager, self).__init__()
    # ------------------------------------------------------------------------
    def lazyLoad(self, source_game: str = "w3"):
        # Mimics CSV is currently W3-only; W2 face animation cataloguing is a
        # separate task. Accept the kwarg to keep the parent signature.
        self._animMeta = CModStoryBoardMimicsListsManager.get_mimics_meta()
        self._extraAnimCount = 0
        self._loadedSourceGame = _normalize_source_game(source_game)
        self._dataLoaded = True
    # ------------------------------------------------------------------------
# ----------------------------------------------------------------------------
