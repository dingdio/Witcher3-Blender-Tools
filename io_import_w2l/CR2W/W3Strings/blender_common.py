try:
    import bpy
    addon_name = "io_import_w2l"
except Exception as e:
    pass
    #raise e
import os

def get_game_path():
    try:
        return bpy.context.preferences.addons[addon_name].preferences.witcher_game_path
    except Exception as e:
        return r"E:\GOG Games\The Witcher 3 Wild Hunt GOTY"
