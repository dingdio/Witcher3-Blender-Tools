# -----------------------------------------------------------------------------
#
# KNOWN BUGS:
#
# ----------------------------------------------------------------------------
# ----------------------------------------------------------------------------

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Tuple, Dict

from .editfield_listview import SModUiListItem

#@dataclass
class SModUiCategorizedListItem:
    def __init__(self
                ,id: str =  ""
                ,caption: str = ""
                ,cat1: str = ""
                ,cat2: str = ""
                ,cat3: str = ""
                ,isWildcardMiss: bool = False):
        # order important for construct calls! do not change!
        self.id: str = id
        self.caption: str = caption
        self.cat1: str = cat1
        self.cat2: str = cat2
        self.cat3: str = cat3
        self.isWildcardMiss: bool = isWildcardMiss
        # flag for ignoring by currently set filter

#@dataclass
class SModUiFilteredListCatItem:
    def __init__(self
                ,id: str  = ""
                ,item: SModUiListItem  = SModUiListItem()
                ,count: int  = 0
                ,total: int  = 0
                ,isOpen: bool  = False
                ,entryPos: int  = 0):
        self.id: str = id
        self.item: SModUiListItem = item
        self.count: int = count 
        self.total: int = total 
        self.isOpen: bool = isOpen 
        self.entryPos: int = entryPos


def StrFindFirst(str, str2):
    cake = str.find(str2)
    return cake
    if str2.lower() in str.lower():
        return True
    else:
        return False
    #return str2.find(str)

# -------------------------------------------------------------------------r---
class CModUiFilteredList(ABC):
    """docstring for CModUiFilteredList."""

    def __init__(self):
        super(CModUiFilteredList, self).__init__()
        self._items: List[SModUiCategorizedListItem] = []
        self._selectedCat1: str = ''
        self._selectedCat2: str = ''
        self._selectedCat3: str = ''

        self._wildcardFilter: str = ''
        # number of items available in filtered list
        self._itemsMatching: int = 0

        self._filteredList: List[SModUiListItem] = []
        self._selectedId: str = ''
    # ------------------------------------------------------------------------
    def setSelection(self, id: str, openCategories: bool = False) -> bool:
        prefix: str
        catId: str
        i: int

        if (id.startswith('CAT')):
            prefix = id[:4] #StrLeft(id, 4)
            catId = id[4:] #StrMid(id, 4)

            if prefix == "CAT1":
                self._selectedCat1 = catId
                self._selectedCat2 = ""
                self._selectedCat3 = ""
            if prefix == "CAT2":
                self._selectedCat2 = catId
                self._selectedCat3 = ""
            if prefix == "CAT3":
                self._selectedCat3 = catId

            return False
        else:
            # normal selection
            self._selectedId = id
            if (openCategories):
                # not pretty but no other option get categories..
                for i in range(0, len(self._items)):
                    if (self._items[i].id == self._selectedId):
                        self.selectedCat1 = self._items[i].cat1
                        self.selectedCat2 = self._items[i].cat2
                        self.selectedCat3 = self._items[i].cat3
            return True
    # ------------------------------------------------------------------------
    def getSelection(self) -> str:
        return self._selectedId
    # ------------------------------------------------------------------------
    def getWildcardFilter(self) -> str:
        return self._wildcardFilter
    # ------------------------------------------------------------------------
    def setWildcardFilter(self, filter: str, ignoreCategories: bool = False):
        isMatch: bool = False
        firstMatchFound: bool = False
        i: int = 0

        self._wildcardFilter = filter
        ignoreCategories = True #!!!!!!!!! TEMP
        for i in range(0, len(self._items)):
            isMatch = False
            if (not ignoreCategories):
                isMatch = StrFindFirst(self._items[i].cat1, self._wildcardFilter) >= 0
                isMatch = isMatch or StrFindFirst(self._items[i].cat2, self._wildcardFilter) >= 0
                isMatch = isMatch or StrFindFirst(self._items[i].cat3, self._wildcardFilter) >= 0
            isMatch = isMatch or StrFindFirst(self._items[i].caption, self._wildcardFilter) >= 0
            self._items[i].isWildcardMiss = not isMatch
            
            # if "geralt" in self._items[i].caption:
            #     cake = 234

            # set selected categories from first match to open the categories
            if (not firstMatchFound and isMatch):
                firstMatchFound = True
                self._selectedCat1 = self._items[i].cat1
                self._selectedCat2 = self._items[i].cat2
                self._selectedCat3 = self._items[i].cat3
    # ------------------------------------------------------------------------
    def resetWildcardFilter(self):
        i: int

        for i in range(0, len(self._items)):
            self._items[i].isWildcardMiss = False

        self._wildcardFilter = ""
    # ------------------------------------------------------------------------
    def clearLowestSelectedCategory(self):
        if (self._selectedCat3 != ""):
            self._selectedCat3 = ""
        elif (self._selectedCat2 != ""):
            self._selectedCat2 = ""
        elif (self._selectedCat1 != ""):
            self._selectedCat1 = ""
    # ------------------------------------------------------------------------
    def updateCatStats(
        self,
        itemList: List[SModUiListItem], #! REF OUT
        catData: SModUiFilteredListCatItem, #! REF OUT
        isVisible: bool = False):
        # check if category has an id or
        if (isVisible and catData.id != ""):
            if (catData.count != catData.total):
                catData.item.suffix = " (" + str(catData.count) + "/" + str(catData.total) + ")"
            else:
                catData.item.suffix = " (" + str(catData.total) + ")"
            catData.item.child_count =  str(catData.count)
            if (catData.count > 0):
                # overwrite categoryEntry with updated data
                itemList[catData.entryPos] = catData.item
            else:
                # filter is active -> remove empty categories
                # Note: stats are updated in the same run of the list
                # => deleting entryPos is ok (no change in total order)
                #item_to_remove = [x for x in itemList if hasattr(x, 'entryPos') and x.entryPos == catData.entryPos]
                itemList.remove(catData.item)

    # ------------------------------------------------------------------------
    def addCategoryEntry(
        self,
        catPos: int,
        catId: str,
        selectedCatId: str,
        itemList: List[SModUiListItem], #! REF OUT
        indent: str, #! REF OUT
        isVisible: bool) -> SModUiFilteredListCatItem :
        catData: SModUiFilteredListCatItem = SModUiFilteredListCatItem()

        if (isVisible):
            catData.id = catId
            catData.item = SModUiListItem("CAT" + str(catPos) + catId, catId)

            if (catId == selectedCatId):
                catData.item.prefix = indent + "-"
                catData.isOpen = True
            else:
                catData.item.prefix = indent + "+"

            # empty categories == not categorized
            if (catId != ""):
                itemList.append(catData.item)
                indent += "    "
                # store position so stats can be updated
                catData.entryPos = len(itemList) - 1
        return catData, indent
    # ------------------------------------------------------------------------
    def getFilteredList(self) -> List[SModUiListItem] :
        item: SModUiCategorizedListItem = SModUiCategorizedListItem()
        itemList: List[SModUiListItem] = []
        i: int = 0
        indent: str = 0

        # managing info about currently set categories
        cat1: SModUiFilteredListCatItem = SModUiFilteredListCatItem()
        cat2: SModUiFilteredListCatItem = SModUiFilteredListCatItem()
        cat3: SModUiFilteredListCatItem = SModUiFilteredListCatItem()

        isOpened: bool = False
        lastCategories: str = ""
        itemCategories: str = ""
        selectedCategories: str = ""

        self._itemsMatching = 0

        # setup default none but open if there are no categories at all
        lastCategories = "||"
        isOpened = True
        selectedCategories = self._selectedCat1 + "|" + self._selectedCat2 + "|" + self._selectedCat3

        # will be increased by "  " in every category
        indent = ""
        for i in range(0, len(self._items)):
            item = self._items[i]

            itemCategories = item.cat1 + "|" + item.cat2 + "|" + item.cat3

            if (itemCategories != lastCategories):
                # at least one category changed
                lastCategories = itemCategories
                isOpened = itemCategories == selectedCategories #!VALUE TO OPEN ALL

                if (item.cat1 != cat1.id):
                    # update stats in cat name
                    self.updateCatStats(itemList, cat1, True)
                    self.updateCatStats(itemList, cat2, cat1.isOpen)
                    self.updateCatStats(itemList, cat3, cat1.isOpen and cat2.isOpen)

                    # reset cat stats counter and add category to list
                    indent = ""
                    cat1, indent = self.addCategoryEntry(1, item.cat1, self._selectedCat1, itemList, indent, True)
                    cat2, indent = self.addCategoryEntry(2, item.cat2, self._selectedCat2, itemList, indent, cat1.isOpen)
                    cat3, indent = self.addCategoryEntry(3, item.cat3, self._selectedCat3, itemList, indent, cat1.isOpen and cat2.isOpen)

                elif (item.cat2 != cat2.id):
                    if item.cat1 == "bob-animals" and item.cat2 == "dog":
                        adww =555
                    # update stats in cat name
                    self.updateCatStats(itemList, cat2, cat1.isOpen)
                    self.updateCatStats(itemList, cat3, cat1.isOpen and cat2.isOpen)

                    # reset cat stats counter and add category to list
                    indent = "    "
                    cat2, indent = self.addCategoryEntry(2, item.cat2, self._selectedCat2, itemList, indent, cat1.isOpen)
                    cat3, indent = self.addCategoryEntry(3, item.cat3, self._selectedCat3, itemList, indent, cat1.isOpen and cat2.isOpen)
                elif (item.cat3 != cat3.id):
                    # update stats in cat name
                    self.updateCatStats(itemList, cat3, cat1.isOpen and cat2.isOpen)

                    # reset cat stats counter and add category to list
                    indent = "      "
                    cat3, indent = self.addCategoryEntry(3, item.cat3, self._selectedCat3, itemList, indent, cat1.isOpen and cat2.isOpen)

            # add item if all of item categories are opened
            if (not item.isWildcardMiss):
                if (isOpened):
                    itemList.append(SModUiListItem(
                        item.id, item.caption, item.id == self._selectedId, indent+"        "))
                # count stats event if it is not visible for category stats
                cat1.count += 1
                cat2.count += 1
                cat3.count += 1
                self._itemsMatching += 1
            cat1.total += 1
            cat2.total += 1
            cat3.total += 1
        # update stats of last category set
        self.updateCatStats(itemList, cat1, True)
        self.updateCatStats(itemList, cat2, cat1.isOpen)
        self.updateCatStats(itemList, cat3, cat1.isOpen and cat2.isOpen)

        return itemList
     # ------------------------------------------------------------------------
    def getMatchingItemCount(self) -> int :
        return self._itemsMatching
    # ------------------------------------------------------------------------
    def getTotalCount(self) -> int :
        return len(self._items)
    # ------------------------------------------------------------------------
    def getPreviousId(self) -> str :
        prevMatchSlot: int
        i: int
        j: int

        prevMatchSlot = -1

        for i in range(0, len(self._items)):

            if (self._items[i].id == self._selectedId):
                # if there is already a prev filter match return it
                if (prevMatchSlot >= 0):
                    return self._items[prevMatchSlot].id
                # otherwise it's a wraparound: run from end of list to current
                # position and return first match

                j = len(self._items)
                while j > i:
                    if (not self._items[j].isWildcardMiss):
                        return self._items[j].id
                    j -= 1
                # nothing found => return already selected
                return self._items[i].id

            elif (not self._items[i].isWildcardMiss):
                prevMatchSlot = i
        return ""
    # ------------------------------------------------------------------------
    def getNextId(self) -> str :
        nextMatchSlot: int
        i: int
        j: int

        nextMatchSlot = -1

        # search backwards to save "next" match
        for i in reversed(range(1, len(self._items))):
            if (self._items[i].id == self._selectedId):
                if (nextMatchSlot >= 0):
                    return self._[nextMatchSlot].id
                # otherwise it's a wraparound: start from start to current
                # position and return first match
                for j in range(0, i):
                    if (not self._items[j].isWildcardMiss):
                        return self._items[j].id
                # nothing found => return already selected
                return self._items[i].id

            elif (not self._items[i].isWildcardMiss):
                nextMatchSlot = i
        return ""

    # ------------------------------------------------------------------------

# -----------------------------------------------------------------------------
