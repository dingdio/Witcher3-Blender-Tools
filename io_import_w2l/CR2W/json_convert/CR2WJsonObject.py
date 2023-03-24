

def loadProps(self, args):
    arg:CR2WJsonObject
    for arg in args[0].items():
        theType = arg[0]
        theData = arg[1]
        if theType == '_elements':
            for idx, item in enumerate(theData):
                thing = list(item.keys())
                if '_elements' in thing:
                    theData[idx] = CR2WJsonArray(item)
                elif '_vars' in thing:
                    theData[idx] = CR2WJsonMap(item)
                elif '_value' in thing:
                    theData[idx] = CR2WJsonScalar(item)
            setattr(self, theType, theData)
        elif theType == '_imports':
            setattr(self, theType, theData)
        elif theType == '_embedded':
            setattr(self, theType, theData)
        elif theType == '_properties':
            setattr(self, theType, theData)
        elif theType == '_buffers':
            setattr(self, theType, theData)
        elif theType == '_vars':
            for item in theData.items():
                thing = list(item[1].keys())
                if '_elements' in thing:
                    theData[item[0]] = CR2WJsonArray(item[1])
                elif '_vars' in thing:
                    theData[item[0]] = CR2WJsonMap(item[1])
                elif '_value' in thing:
                    theData[item[0]] = CR2WJsonScalar(item[1])
            setattr(self, theType, theData)
        elif theType == '_chunks':
            theData:dict
            for item in theData.items():
                theData[item[0]] = CR2WJsonChunkMap(item[1])
            setattr(self, theType, theData)
        else:
            setattr(self, theType, theData)

class CR2WJsonObject(object):
    def __init__(self, args):
        self._type = None
        loadProps(self, args)

class CR2WJsonScalar(CR2WJsonObject):
    def __init__(self, *args, **kwargs):
        self._value = None
        if '_type' in kwargs:
            self._type = kwargs['_type']
            self._value = kwargs['_value']
        else:
            super(CR2WJsonScalar, self).__init__(args)

class CR2WJsonArray(CR2WJsonObject):
    def __init__(self, *args, **kwargs):
        self._bufferPadding = None
        self._elements = []
        if '_type' in kwargs:
            self._type = kwargs['_type']
        else:
            super(CR2WJsonArray, self).__init__(args)

class CR2WJsonMap(CR2WJsonObject):
    def __init__(self, *args, **kwargs):
        self._vars: dict = {}
        if '_type' in kwargs:
            self._type:str = kwargs['_type']
        else:
            super(CR2WJsonMap, self).__init__(args)

class CR2WJsonChunkMap(CR2WJsonObject):
    def __init__(self, *args, **kwargs):
        self._key = None
        self._parentKey = None
        self._flags = None
        self._unknownBytes = None
        self._vars: dict = {}
        if '_type' in kwargs:
            self._type = kwargs['_type']
        else:
            super(CR2WJsonChunkMap, self).__init__(args)

class CR2WJsonData(CR2WJsonObject):
    def __init__(self, *args, **kwargs):
        self._extension:str = ''
        self._imports: list[dict] = []
        self._properties: list = []
        self._buffers: list = []
        self._embedded: list[dict] = []
        self._chunks: dict = {}
        if 'create' in kwargs:
            self._type = 'CR2W'
        else:
            super(CR2WJsonData, self).__init__(args)

import os
import json
def getRigTemplate():
    fileDir = os.path.dirname(os.path.realpath(__file__))
    fileDir = os.path.join(fileDir, "CR2WJsonTemplates")
    filename = os.path.join(fileDir, "skelly_template.json")
    with open(filename) as file:
        fileRead = file.read()
        file.close()
        the_json =json.loads(fileRead)
        return CR2WJsonData(the_json)