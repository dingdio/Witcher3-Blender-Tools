from typing import List
from io_import_w2l.filtered_list.witcher_classes import CName

class SSbUiExtraAnimation(object):
    """docstring for SSbUiExtraAnimation."""
    def __init__(self
                ,animId
                ,animName
                ,subCategory1
                ,subCategory2
                ,caption
                ,frames):
        super(SSbUiExtraAnimation, self).__init__()
        # unique, numerical id for this animation
        # Note: 100000 will be added to this values so it does not conflict with
        # vanilla animations. this id MUST be unique.
        # Note: see additional info below if the animation has to be usable in
        # radish scene encoder
        self.animId: int = animId
        # name identifying the animation. this is used to start the animation with
        # scripts in sbui.
        # Note: the name should be unique in the set of all animations assigned to
        # an actor template. it does not need necessarily to be unique in the set
        # of all animations in the game (e.g. there are many different walking
        # animations for different skeletons/templates (monsters, animals, man,
        # women, ...) all referenced with the aninName 'walk'. but for every
        # spawned actor there is only one unambigues 'walk' animation).
        self.animName: CName = animName
        # optional sub category 1 to group animations
        self.subCategory1: str = subCategory1
        # optional sub category 2 to group animations
        self.subCategory2: str = subCategory2
        # caption for animation in sbui selection list
        self.caption: str = caption
        # number of frames of this animation
        self.frames: int = frames

# ----------------------------------------------------------------------------
# Note: compatibility with radish scene encoder
# ---------------------------------------------
# if custom animations should be usable with radish scene encoder additional
# information must be provided for the radish encoder in a repository file.
# see
#      <radish modding tools>/repos.scenes/sbui.custom_animations.repo.yml
#      <radish modding tools>/repos.scenes/sbui.custom_mimics.repo.yml
# for more information
# ----------------------------------------------------------------------------
# add custom mod animations for actors here:
# ----------------------------------------------------------------------------
def SBUI_getExtraAnimations() -> List[SSbUiExtraAnimation]:
    anims: List[SSbUiExtraAnimation] = []

    # Note: order must be sorted by subCategory1, subCategory2,
    #anims.append(SSbUiExtraAnimation(1, 'man_work_sit_table_sleep_stop', "work", "man", "new anim caption", 110));
    #anims.append(SSbUiExtraAnimation(2, 'geralt_reading_book_loop_01', "idle", , "new idle anim caption", 300));
    #anims.append(SSbUiExtraAnimation(321, 'fancy_animname', , , "new fancy anim", 123));

    return anims
# ----------------------------------------------------------------------------
def SBUI_getExtraMimics() -> List[SSbUiExtraAnimation]:
    mimicAnims: List[SSbUiExtraAnimation] = []

    # Note: order must be sorted by subCategory1, subCategory2,
    # mimicAnims.append(SSbUiExtraAnimation(1, 'geralt_neutral_gesture_eating_face', "man", , "new mimics caption", 259));
    # mimicAnims.append(SSbUiExtraAnimation(321, 'fancy_mimicanimname', , , "new fancy mimicanim", 123));

    return mimicAnims

# ----------------------------------------------------------------------------
def SBUI_getExtraIdleAnimations() -> List[SSbUiExtraAnimation]:
    idleAnims: List[SSbUiExtraAnimation] = []

    # Note: these animations must also be available as "normal" animation with
    # the same ids as above (also in the sbui.custom_animations.repo.yml!)

    # Note: order must be sorted by subCategory1, subCategory2,
    # idleAnims.append(SSbUiExtraAnimation(2, 'geralt_reading_book_loop_01', , , "new idle anim caption", 300));
    # idleAnims.append(SSbUiExtraAnimation(321, 'fancy_animname', , , "new fancy anim", 123));

    return idleAnims
# ----------------------------------------------------------------------------
