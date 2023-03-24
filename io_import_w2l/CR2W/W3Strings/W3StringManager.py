import os
import time
import json
from pathlib import Path
from ..bStream import *
from .W3StringFile import W3StringFile
from .W3StringBlock1 import W3StringBlock1
from .blender_common import get_game_path


class Configuration:
    TextLanguage = "en"
    VoiceLanguage = "en"
    ExecutablePath = get_game_path()

class W3StringManager():
    InstanceManager = None
    def __init__(self):
        self.Language  = 'en'
        self.Lines:dict = {}
        self.Keys:dict = {}
        #self.importedStrings = {} #TODO
    
    def Load(self, newlanguage:str, path:str, onlyIfLanguageChanged:bool = False):
        if (onlyIfLanguageChanged and self.Language == newlanguage):
            return

        self.Language = newlanguage
        self.Lines:dict = {}
        self.Keys:dict = {}
        
        gamedir = Path(path)
        content = gamedir# / "content"
        for file in list(content.rglob(self.Language+'.w3strings')):
            self.OpenFile(file)
    def OpenFile(self, filePath):
        try:
            stringFile = W3StringFile()
            stream = bStream(path = filePath.absolute())
            stringFile.Read(stream)
        except Exception as e:
            raise e
        for item in stringFile.block1:
            if item.str_id not in self.Lines:
                self.Lines[item.str_id] = []
            self.Lines[item.str_id].append(item)
        for item in stringFile.block2:
            if item.str_id not in self.Keys:
                self.Keys[item.str_id] = True

    def GetString(self, id: int):
        if (id in self.Lines):
            arr = self.Lines[id]
            return arr[len(arr) - 1].str

        return None

    @classmethod
    def from_json(cls, data):
        t_class = cls()
        t_class.Language = data['Language']
        for line in data['Lines'].items():
            t_class.Lines[int(line[0])] = list(map(W3StringBlock1.from_json, line[1]))
        for line in data['Keys'].items():
            t_class.Keys[int(line[0])] = line[1]
        return t_class
    @staticmethod
    def Get():
        if (W3StringManager.InstanceManager == None):
            fileDir = os.path.dirname(os.path.realpath(__file__))
            filename = os.path.join(fileDir, "string_cache.json")
            
            start_time = time.time()
            if not os.path.exists(filename):
                w3StringManager = W3StringManager()
                w3StringManager.Load(Configuration.TextLanguage, Configuration.ExecutablePath)
                with open(filename, "w") as file:
                    file.write(json.dumps(w3StringManager,default=vars, sort_keys=False, separators=(',', ":")))
            else:
                file_data = open(filename, "r", 1).read()
                json_data = json.loads(file_data)
                w3StringManager = W3StringManager.from_json(json_data)
            time_taken = time.time() - start_time
            print(f'Loaded Strings in {time_taken} seconds.')
            W3StringManager.InstanceManager = w3StringManager
        return W3StringManager.InstanceManager