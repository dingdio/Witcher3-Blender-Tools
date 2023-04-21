try:
    import bpy
    addon_name = "io_import_w2l"
except Exception as e:
    pass
    #raise e
import os

def repo_file(filepath: str, version = 999):
    
    try:
        fbx_uncook_path = bpy.context.preferences.addons[addon_name].preferences.fbx_uncook_path
        uncook_path = bpy.context.preferences.addons[addon_name].preferences.uncook_path
        
        if version <= 115:
            fbx_uncook_path = bpy.context.preferences.addons[addon_name].preferences.fbx_uncook_path
            uncook_path = bpy.context.preferences.addons[addon_name].preferences.witcher2_game_path + '\\data'
    except Exception as e:
        fbx_uncook_path = "E:\\w3_uncook\\FBXs"
        uncook_path = "E:\\w3.modding\\modkit\\r4data"
        if version <= 115:
            fbx_uncook_path = "E:\\w3_uncook\\FBXs"
            uncook_path = "G:\\GOG Games\\The Witcher 2\\data"

    if filepath.endswith('.fbx'):
        return os.path.join(fbx_uncook_path, filepath)
    else:
        return os.path.join(uncook_path, filepath)

def get_game_path():
    try:
        return bpy.context.preferences.addons[addon_name].preferences.witcher_game_path
    except Exception as e:
        return r"E:\GOG Games\The Witcher 3 Wild Hunt GOTY"
