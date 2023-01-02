import os
import sys
from .third_party_libs import yaml
from .common_blender import repo_file
from .setup_logging import *
log = logging.getLogger(__name__)

parent_path = os.path.abspath(os.path.join(os.path.abspath(__file__), os.pardir))
sys.path.append(parent_path.replace("\CR2W", ""))
import io

from .bin_helpers import (ReadUlong48, readUShort,
                        readFloat,
                        ReadFloat24,
                        ReadFloat16)

from .CR2W_types import ( Entity_Type_List, getCR2W, CLASS )

from .bStream import *

class ReadCompressFloat():
    def __init__(self, f, compression):
        val = 0;
        if (compression == 0):
            val = readFloat(f)
        if (compression == 1):
            val = ReadFloat24(f)
        if (compression == 2):
            val = ReadFloat16(f)
        self.val = val

class LayerGroup(object):
    yaml_loader = yaml.SafeLoader
    yaml_tag = u'!LayerGroup'

    def __init__(self, name = "GROUP_NAME"):
        self.name = name
        self.ChildrenGroups = []
        self.ChildrenInfos = []


class CLayerInfo(object):
    def __init__(self, name = "", depotFilePath = "", layerBuildTag = ""):
        self.name = name
        self.depotFilePath = depotFilePath
        self.layerBuildTag = layerBuildTag # enumb check wolvenkit fix in _types

class WORLD:
    def __init__(self):
        self.worldName = "WORLD_NAME"
        self.terrainClipMap = None
        self.tileRes:int = 256
        self.terrainSize:float = 2000
        self.lowestElevation:float = 0
        self.highestElevation:float = 100
        self.groups = []

def write_yml(world):
    with open(r"E:\w3_uncook\export_yml\blender_test.yaml", "w") as f:

        # layerName = 'architecture'
        # worldName = world.worldName
        # dict = {}
        # dict['layers'] = {}
        # dict['layers'][layerName] = {}
        # dict['layers'][layerName]['statics'] = {}
        # dict['layers'][layerName]['statics']
        # dict['layers'][layerName]["world"] = worldName
        # for group in world.groups:
        #     dict['layers'][layerName]['statics'][group.name] = {}
        #     dict['layers'][layerName]['statics'][group.name]['.type'] = "CEntity"
        #     dict['layers'][layerName]['statics'][group.name]['path'] = r"c:\cake\cake.lvl"
        # yaml.dump(dict, f, indent=None)
        yaml.dump(world, f, indent=None, default_flow_style=False)

def getChildrenInfos(info, CHUNKS):
    tag = info.GetVariableByName('layerBuildTag')
    if tag:
        layerBuildTag = info.GetVariableByName('layerBuildTag').Index.String
    else:
        layerBuildTag = None
    name = info.GetVariableByName('shortName')
    if name:
        shortName = info.GetVariableByName('shortName').String.String
    else:
        shortName = None
    if info.GetVariableByName('depotFilePath'):
        depotFilePath = info.GetVariableByName('depotFilePath').String.String
    else:
        depotFilePath = "ERROR"
    info_obj = CLayerInfo(shortName,depotFilePath, layerBuildTag)
    return info_obj

def getChildrenGroups(group, CHUNKS):
    groupName = group.GetVariableByName('name').String.String
    group_obj = LayerGroup(groupName)
    if group.ChildrenGroups:
        for ChildGroup in group.ChildrenGroups:
            group_obj.ChildrenGroups.append(getChildrenGroups(ChildGroup.GetRef(CHUNKS), CHUNKS))
    if group.ChildrenInfos:
        for ChildInfo in group.ChildrenInfos:
            group_obj.ChildrenInfos.append(getChildrenInfos(ChildInfo.GetRef(CHUNKS), CHUNKS))
    return group_obj

def create_world(file):

    world = WORLD()
    CHUNKS = file.CHUNKS.CHUNKS
    for chunk in CHUNKS:
        if chunk.name == "CGameWorld":
            CGameWorld:CLASS = chunk
    firstLayer = CHUNKS[CGameWorld.Firstlayer.Reference]
    
    world.terrainClipMap = CHUNKS[CGameWorld.GetVariableByName('terrainClipMap').Value-1]
    #world.clipSize:int = world.terrainClipMap.GetVariableByName('clipSize').Value
    #world.clipmapSize:int = world.terrainClipMap.GetVariableByName('clipmapSize').Value
    world.tileRes:int = world.terrainClipMap.GetVariableByName('tileRes').Value
    world.terrainSize:float = world.terrainClipMap.GetVariableByName('terrainSize').Value
    world.lowestElevation:float = world.terrainClipMap.GetVariableByName('lowestElevation').Value
    world.highestElevation:float = world.terrainClipMap.GetVariableByName('highestElevation').Value
    
    world.groups = getChildrenGroups(firstLayer, CHUNKS)
    world.worldName = world.groups.name
    # for ChildGroup in firstLayer.ChildrenGroups:
    #     groupName = ChildGroup.GetRef(CHUNKS).GetVariableByName('name').String.String
    #     print(groupName);
    #     world.groups.append(LayerGroup(groupName))
    return world

#Top level entity of a file. Represents Clayer or CEntityTemplate
class LEVEL():
    def __init__(self):
        self.layerNode = "LEVEL_NAME"
        self.CSectorData = {}
        self.Entities = []
        self.includes = []
        self.Foliage = False
        self.type = "Clayer"

class CLayerInfo(object):
    def __init__(self, name = "", depotFilePath = "", layerBuildTag = ""):
        self.name = name
        self.depotFilePath = depotFilePath
        self.layerBuildTag = layerBuildTag # enumb check wolvenkit fix in _types

class CEntity():
    def __init__(self):
        self.name = "CEntity"
        self.type = "CEntity"
        self.transform = False
        self.template = False
        self.templatePath = False
        self.Components = []
        self.BufferV1 = False
        self.BufferV2 = False
        self.isCreatedFromTemplate = False
        self.streamingDataBuffer = False

    def show(self):
        print("Inside Parent")

class CGameplayEntity(CEntity):
    def __init__(self):
        super().__init__()
        self.name = "CGameplayEntity"
        self.type = "CGameplayEntity"

    def show(self):
        super().show()

class CItemEntity(CEntity):
    def __init__(self):
        super().__init__()
        self.name = "CItemEntity"
        self.type = "CItemEntity"

    def show(self):
        super().show()
        
        
class CWitcherSword(CEntity):
    def __init__(self):
        super().__init__()
        self.name = "CWitcherSword"
        self.type = "CWitcherSword"

    def show(self):
        super().show()


#Container Entities
class W3LockableEntity(CGameplayEntity):
    def __init__(self):
        super().__init__()
        self.name = "W3LockableEntity"
        self.type = "W3LockableEntity"

    def show(self):
        super().show()

class W3Container(W3LockableEntity):
    def __init__(self):
        super().__init__()
        self.name = "W3Container"
        self.type = "W3Container"

    def show(self):
        super().show()

class W3AnimatedContainer(W3Container):
    def __init__(self):
        super().__init__()
        self.name = "W3AnimatedContainer"
        self.type = "W3AnimatedContainer"

    def show(self):
        super().show()

#DOOR ENTITIES
class W3LockableEntity(CGameplayEntity):
    def __init__(self):
        super().__init__()
        self.name = "W3LockableEntity"
        self.type = "W3LockableEntity"

    def show(self):
        super().show()

class W3NewDoor(W3LockableEntity):
    def __init__(self):
        super().__init__()
        self.name = "W3NewDoor"
        self.type = "W3NewDoor"

    def show(self):
        super().show()

def create_level(file, filename):
    level = LEVEL();
    level.layerNode = filename
    CHUNKS = file.CHUNKS.CHUNKS
    CSectorData =  False
    Entities = []
    level.name = CHUNKS[0].name
    level.type = CHUNKS[0].name

    #only create a LEVEL for these types otherwise return entire file
    top_level_list = [ "CLayer", "CEntityTemplate", "CFoliageResource" ]
    if level.type not in top_level_list:
        return file
        

    module = __import__("CR2W")
    for chunk in CHUNKS:
        if chunk.name == "CFoliageResource":
            level.Foliage = chunk
            
        if chunk.name == "CEntityTemplate":
            includes = chunk.GetVariableByName('includes')
            if includes:
                for include in includes.Handles:
                    try:
                        fileName = repo_file(include.DepotPath)
                        CR2WFile = read_CR2W(fileName)
                        entity = create_level(CR2WFile, fileName)
                        level.includes.append(entity)
                    except Exception as e:
                        log.exception("Problem Importing an include")
        if chunk.name == "CSectorData":
            CSectorData = chunk
        if chunk.name in Entity_Type_List:
            class_ = getattr(module.CR2W_file, chunk.name)
            Entity = class_()

            if chunk.GetVariableByName('transform'):
                Entity.transform = chunk.GetVariableByName('transform').EngineTransform
            #each transform should have it's own equlivant blender object.
            if hasattr(chunk, "isCreatedFromTemplate") and chunk.isCreatedFromTemplate:
                Entity.BufferV2 = chunk.BufferV2
                Entity.isCreatedFromTemplate = chunk.isCreatedFromTemplate
                #broken template? Need to read include files if can't find mesh buffer?
                if chunk.Template.Handles[0].DepotPath == r"gameplay\containers\new_locations\novigrad\indoors\average\simple_dresser_table.w2ent":
                    chunk.Template.Handles[0].DepotPath = r"environment\decorations\containers\dressers\simple_dresser\simple_dresser_table.w2ent"
                fileName = repo_file(chunk.Template.Handles[0].DepotPath)
                CR2WFile = read_CR2W(fileName)
                entity = create_level(CR2WFile, fileName)
                Entity.template = entity
                Entity.templatePath = chunk.Template.Handles[0].DepotPath
                Entities.append(Entity)
            else:
                #TODO CHECK WHEN HAPPENS
                #TODO FIX ENTITY READING
                if chunk.GetVariableByName('streamingDataBuffer'):
                    Bufferdata = chunk.GetVariableByName('streamingDataBuffer').Bufferdata
                    f = bStream(data = bytearray(Bufferdata.Bytes))
                    f.name = "DATA_BUFFER"
                    bufferedCR2W = getCR2W(f)
                    entity = create_level(bufferedCR2W, chunk.name)
                    Entity.streamingDataBuffer = entity

                if hasattr(chunk, 'Components'):
                    for chunk_id in chunk.Components:
                        sub_chunk  = CHUNKS[chunk_id-1]
                        if sub_chunk.name == "CPointLightComponent":
                            Entity.Components.append(sub_chunk)
                        if sub_chunk.name == "CSpotLightComponent":
                            Entity.Components.append(sub_chunk)
                        if sub_chunk.name == "CMeshComponent":
                            Entity.Components.append(sub_chunk)
                Entities.append(Entity)
    level.CSectorData = CSectorData
    level.Entities = Entities

    return level

def read_CR2W(filename): #lipsync load face first
    with open(filename,"rb") as f:
        theFile = getCR2W(f)
        f.close()
    # f = bStream(path = filename)
    # theFile = getCR2W(f)
    # f.close()
    return theFile
