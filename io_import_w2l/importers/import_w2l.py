from io_import_w2l.CR2W.CR2W_helpers import Enums
from io_import_w2l.importers.import_blender_fun import loadLevel
from io_import_w2l.importers.import_helpers import MatrixToArray, get_entity_data, get_w3_level_data, levelExportData, meshPath

# def btn_import_W2L(level, fbx_uncook_path = "E:\\w3_uncook\\FBXs"):
#     mesh_objects = []
#     mesh_paths = []
#     for block in level.CSectorData.BlockData:
#         if block.packedObjectType == Enums.BlockDataObjectType.Mesh:
#             mesh_objects.append(block)
#             mesh_path = level.CSectorData.Resources[block.packedObject.meshIndex].pathHash
#             mesh_paths.append(meshPath(mesh_path, block.position, MatrixToArray(block.rotationMatrix), fbx_uncook_path ))
#     print("cake")

#     w3_level_data = levelExportData(level.layerNode, mesh_paths)
#     loadLevel(w3_level_data)
def btn_import_W2L(level, fbx_uncook_path = "E:\\w3_uncook\\FBXs"):
    loadLevel(level)
    #w3_level_data = get_w3_level_data(level)
    #loadLevel(w3_level_data)

def btn_import_w2ent(level, fbx_uncook_path = "E:\\w3_uncook\\FBXs"):
    loadLevel(level)
    #w3_level_data = get_entity_data(level)
    #loadLevel(w3_level_data)

if __name__ == "__main__":
    btn_import_W2L()