from ..CR2W.CR2W_helpers import Enums
from ..importers.import_blender_fun import loadLevel
from ..importers.import_helpers import MatrixToArray, get_entity_data, get_w3_level_data, levelExportData, meshPath

def btn_import_W2L(level, context=None, keep_lod_meshes = False, **kwargs):
    loadLevel(level, context, keep_lod_meshes, **kwargs)

if __name__ == "__main__":
    btn_import_W2L()
