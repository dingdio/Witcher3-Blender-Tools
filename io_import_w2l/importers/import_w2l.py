from io_import_w2l.CR2W.CR2W_helpers import Enums
from io_import_w2l.importers.import_blender_fun import loadLevel
from io_import_w2l.importers.import_helpers import MatrixToArray, get_entity_data, get_w3_level_data, levelExportData, meshPath

def btn_import_W2L(level, context=None, keep_lod_meshes = False, **kwargs):
    loadLevel(level, context, keep_lod_meshes, **kwargs)

def btn_import_w2ent(level, context=None, keep_lod_meshes = False):
    loadLevel(level, context)
    #w3_level_data = get_entity_data(level)
    #loadLevel(w3_level_data)

if __name__ == "__main__":
    btn_import_W2L()