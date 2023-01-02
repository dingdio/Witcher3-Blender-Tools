from animations_manager import CModStoryBoardActor, CModStoryBoardAnimationListsManager


animListsManager: CModStoryBoardAnimationListsManager = CModStoryBoardAnimationListsManager()

actor = CModStoryBoardActor()

animListsManager.lazyLoad()


#TODO list should be filtered by the list of w2anims passed into it from the entity object
list = animListsManager.getAnimationListFor(actor)
list.setWildcardFilter("")
filteredList = list.getFilteredList()
print(list.getMatchingItemCount(),"/",list.getTotalCount())