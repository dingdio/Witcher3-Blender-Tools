from .Types.BlenderMesh import CommonData
from .Types.SBufferInfos import SBufferInfos, SVertexBufferInfos
from .setup_logging import *
log = logging.getLogger(__name__)

from .CR2W_types import getCR2W

def load_bin_mesh(filename):
    #raise NotImplementedError
    log.info('FileLoading: '+ filename)

    with open(filename,"rb") as f:
        meshFile = getCR2W(f)
        f.close()

    CData:CommonData = CommonData()
    bufferInfos:SBufferInfos = SBufferInfos()
    for chunk in meshFile.CHUNKS.CHUNKS:
        if chunk.Type == "CMesh":
            vertexBufferInfos = SVertexBufferInfos()
            cookedDatas = chunk.GetVariableByName("cookedData")
            ckae = 123
        if chunk.Type == "CMaterialInstance":
            CData.materialInstances.append(chunk)
            pass

    meshData = meshFile
    return meshData