from .CollisionManager import CollisionManager
def LoadCollisionManager(do_reload=False, loadmods=False):
    try:
        return CollisionManager.Get(do_reload=do_reload, loadmods=loadmods)
    except Exception as e:
        raise e
