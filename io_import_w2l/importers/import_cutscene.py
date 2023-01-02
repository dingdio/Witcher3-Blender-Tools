import os
import json
from io_import_w2l.CR2W import read_json_w3

def loadCutsceneFile(filename):
    dirpath, file = os.path.split(filename)
    basename, ext = os.path.splitext(file)
    if ext.lower() in ('.json'):
        with open(filename) as file:
            return read_json_w3.Read_CCutsceneTemplate(json.loads(file.read()))
    else:
        return None

def import_w3_cutscene(filename):
    CCutsceneTemplate = loadCutsceneFile(filename)
    return CCutsceneTemplate

