from .setup_logging import *
log = logging.getLogger(__name__)

from .CR2W_types import getCR2W

def load_bin_mesh(filename):
    #raise NotImplementedError
    log.info('FileLoading: '+ filename)

    with open(filename,"rb") as f:
        theFile = getCR2W(f)
        f.close()

    meshData = False
    return meshData