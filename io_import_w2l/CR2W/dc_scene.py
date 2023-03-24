import json
from .w3_types import Quaternion, Vector3D, CStoryScene
#from .read_json_w3 import readCsceneData
from .CR2W_types import getCR2W, W_CLASS



def create_scene(file):
    storyScene = CStoryScene()
    storyScene.chunksRef = file.CHUNKS.CHUNKS
    storyScene.LocalizedStringsRef = file.LocalizedStrings
    chunk:W_CLASS
    for chunk in file.CHUNKS.CHUNKS:
        if chunk.name == "CStoryScene":
            for prop in chunk.PROPS:
                setattr(storyScene, prop.theName, prop)
            #storyScene.sceneTemplates = chunk.GetVariableByName('sceneTemplates')
        elif chunk.name == "CStorySceneLine":
            #skelly = read_skelly(chunk)
            break
    return storyScene #skelly


def load_bin_scene(fileName):
    with open(fileName,"rb") as f:
        theFile = getCR2W(f)
        f.close()
    return create_scene(theFile)