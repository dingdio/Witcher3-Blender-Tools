import logging
import os
from pathlib import Path
from .third_party_libs import yaml
from .common_blender import repo_file
from .prop_utils import prop_to_string
from ..extension_paths import get_dev_override
log = logging.getLogger(__name__)
import io

from .bin_helpers import (ReadUlong48, readUShort,
                        readFloat,
                        ReadFloat24,
                        ReadFloat16)

from .CR2W_types import ( Entity_Type_List, getCR2W, W_CLASS )

from .bStream import *


def _stream_chunk_props_summary(chunk, limit=8):
    props = getattr(chunk, "PROPS", None) or []
    out = []
    for prop in props[:limit]:
        out.append(f"{getattr(prop, 'theName', '?')}:{getattr(prop, 'theType', '?')}")
    return ", ".join(out)


def _first_handle_depot_path(prop):
    handles = getattr(prop, "Handles", None) or []
    if not handles:
        return ""
    return str(getattr(handles[0], "DepotPath", "") or "").strip()


def _stream_chunk_string_prop(stream_chunk, *prop_names):
    if stream_chunk is None:
        return ""
    stream_type = str(getattr(stream_chunk, "Type", getattr(stream_chunk, "name", "")) or "").strip()
    for prop_name in prop_names:
        prop = stream_chunk.GetVariableByName(prop_name) if hasattr(stream_chunk, "GetVariableByName") else None
        value = str(prop_to_string(prop) or "").strip()
        if not value:
            continue
        if value == stream_type:
            continue
        return value
    return ""


def _should_suppress_streaming_name_warning(stream_chunk, resource_path="", mesh_path=""):
    if resource_path or mesh_path:
        return False
    stream_type = getattr(stream_chunk, "Type", getattr(stream_chunk, "name", "")) if stream_chunk else ""
    return stream_type in {
        "CEffectDummyComponent",
        "CInteractionComponent",
        "CSoundEmitterComponent",
    }


def _describe_streaming_name_failure(file, entity_chunk, stream_chunk, resource_prop, mesh_prop):
    parts = []
    file_name = getattr(file, "fileName", None)
    if file_name:
        parts.append(f"file={file_name}")
    if entity_chunk is not None:
        parts.append(
            f"entity_chunk={getattr(entity_chunk, 'ChunkIndex', '?')}:{getattr(entity_chunk, 'name', getattr(entity_chunk, 'Type', '?'))}"
        )
    if stream_chunk is not None:
        parts.append(
            f"stream_chunk={getattr(stream_chunk, 'ChunkIndex', '?')}:{getattr(stream_chunk, 'Type', getattr(stream_chunk, 'name', '?'))}"
        )
        props = _stream_chunk_props_summary(stream_chunk)
        if props:
            parts.append(f"props={props}")
    parts.append(f"resource_handles={len(getattr(resource_prop, 'Handles', None) or [])}")
    parts.append(f"mesh_handles={len(getattr(mesh_prop, 'Handles', None) or [])}")
    return ", ".join(parts)


def _extract_streaming_buffer_bytes(cr2w_file, streaming_data_prop):
    if not streaming_data_prop:
        return None
    buffer_data = getattr(streaming_data_prop, "Bufferdata", None)
    if buffer_data is not None and hasattr(buffer_data, "Bytes"):
        return buffer_data.Bytes
    buffer_index = int(getattr(streaming_data_prop, "ValueA", 0) or 0) - 1
    file_buffers = getattr(cr2w_file, "BufferData", None) or []
    if 0 <= buffer_index < len(file_buffers):
        return file_buffers[buffer_index]
    return None

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
        self.clipSize:int = 0
        self.clipmapSize:int = 0
        self.terrainSize:float = 2000
        self.lowestElevation:float = 0
        self.highestElevation:float = 100
        self.groups = []

def write_yml(world, output_path=None):
    if output_path is None:
        output_path = get_dev_override("cr2w_write_yml_output", "")
    if not output_path:
        raise RuntimeError("No output path provided for write_yml")

    with open(output_path, "w") as f:

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
        #     dict['layers'][layerName]['statics'][group.name]['path'] = r"<depot/path/to/file>"
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
            CGameWorld:W_CLASS = chunk
    firstLayer = CHUNKS[CGameWorld.Firstlayer.Reference]
    
    world.terrainClipMap = CHUNKS[CGameWorld.GetVariableByName('terrainClipMap').Value-1]
    clip_size = world.terrainClipMap.GetVariableByName('clipSize')
    if clip_size:
        world.clipSize = clip_size.Value
    clipmap_size = world.terrainClipMap.GetVariableByName('clipmapSize')
    if not clipmap_size:
        clipmap_size = world.terrainClipMap.GetVariableByName('clipMapSize')
    if clipmap_size:
        world.clipmapSize = clipmap_size.Value
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
        log.debug("Inside Parent")

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


class Crossbow(CEntity):
    def __init__(self):
        super().__init__()
        self.name = "Crossbow"
        self.type = "Crossbow"

    def show(self):
        super().show()


class CWitcherJacket(CItemEntity):
    def __init__(self):
        super().__init__()
        self.name = "CWitcherJacket"
        self.type = "CWitcherJacket"

    def show(self):
        super().show()


class CWitcherPants(CItemEntity):
    def __init__(self):
        super().__init__()
        self.name = "CWitcherPants"
        self.type = "CWitcherPants"

    def show(self):
        super().show()


class CWitcherBoots(CItemEntity):
    def __init__(self):
        super().__init__()
        self.name = "CWitcherBoots"
        self.type = "CWitcherBoots"

    def show(self):
        super().show()

#Actor Entities
class CActor(CGameplayEntity):
    def __init__(self):
        super().__init__()
        self.name = "CActor"
        self.type = "CActor"

    def show(self):
        super().show()
        
class CNewNPC(CActor):
    def __init__(self):
        super().__init__()
        self.name = "CNewNPC"
        self.type = "CNewNPC"

    def show(self):
        super().show()
        
class CPlayer(CActor):
    def __init__(self):
        super().__init__()
        self.name = "CPlayer"
        self.type = "CPlayer"

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

## WITCHER 2 Classes
#CDoor
#CContainer

class CDoor(W3LockableEntity):
    def __init__(self):
        super().__init__()
        self.name = "W3NewDoor"
        self.type = "W3NewDoor"

    def show(self):
        super().show()

class CContainer(W3LockableEntity):
    def __init__(self):
        super().__init__()
        self.name = "CContainer"
        self.type = "CContainer"

    def show(self):
        super().show()

class CActionPoint(W3LockableEntity):
    def __init__(self):
        super().__init__()
        self.name = "CActionPoint"
        self.type = "CActionPoint"

    def show(self):
        super().show()


from . import CR2W_file


def _load_level_dependency(resolved_path, dependency_loader=None, dependency_resolver=None):
    if not resolved_path:
        return None
    if dependency_loader is not None:
        return dependency_loader(resolved_path)
    cr2w_file = read_CR2W(resolved_path)
    return create_level(
        cr2w_file,
        resolved_path,
        dependency_loader=dependency_loader,
        dependency_resolver=dependency_resolver,
    )

def _resolve_level_dependency_path(depot_path, version, dependency_resolver=None):
    if not depot_path:
        return ""
    if dependency_resolver is not None:
        try:
            resolved = dependency_resolver(depot_path, version)
        except TypeError:
            resolved = dependency_resolver(depot_path)
        if resolved:
            return resolved
    return repo_file(depot_path, version)


def create_level(file, filename, dependency_loader=None, dependency_resolver=None):
    level = LEVEL()
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
        
    for chunk in CHUNKS:
        if chunk.name == "CFoliageResource":
            level.Foliage = chunk
            
        if chunk.name == "CEntityTemplate":
            includes = chunk.GetVariableByName('includes')
            if includes and hasattr(includes, 'Handles'): #!TODO witcher2 includes
                for include in includes.Handles:  ## array:2,0,#CEntityTemplate WITCHER2
                    try:
                        include_path = getattr(include, "DepotPath", None)
                        if not include_path:
                            continue
                        fileName = _resolve_level_dependency_path(
                            include_path,
                            file.HEADER.version,
                            dependency_resolver=dependency_resolver,
                        )
                        entity = _load_level_dependency(
                            fileName,
                            dependency_loader=dependency_loader,
                            dependency_resolver=dependency_resolver,
                        )
                        if entity is None:
                            continue
                        level.includes.append(entity)
                    except Exception as e:
                        log.exception("Problem Importing an include")
        if chunk.name == "CSectorData":
            CSectorData = chunk
        if chunk.name in Entity_Type_List:
            try:
                class_ = getattr(CR2W_file, chunk.name)
            except Exception as e:
                log.critical(f'Found undefined entity class "{chunk.name}", skipping')
                #raise e
                continue
            Entity = class_()
            Entity.name = chunk.get_name_prop_string()
            if chunk.GetVariableByName('transform'):
                Entity.transform = chunk.GetVariableByName('transform').EngineTransform
            #each transform should have it's own equlivant blender object.
            if hasattr(chunk, "isCreatedFromTemplate") and chunk.isCreatedFromTemplate:
                Entity.BufferV2 = chunk.BufferV2
                Entity.isCreatedFromTemplate = chunk.isCreatedFromTemplate
                #broken template? Need to read include files if can't find mesh buffer?
                if chunk.Template.Handles[0].DepotPath == r"gameplay\containers\new_locations\novigrad\indoors\average\simple_dresser_table.w2ent":
                    chunk.Template.Handles[0].DepotPath = r"environment\decorations\containers\dressers\simple_dresser\simple_dresser_table.w2ent"
                template_path = getattr(chunk.Template.Handles[0], "DepotPath", None)
                if not template_path:
                    continue
                try:
                    fileName = _resolve_level_dependency_path(
                        template_path,
                        file.HEADER.version,
                        dependency_resolver=dependency_resolver,
                    )
                    entity = _load_level_dependency(
                        fileName,
                        dependency_loader=dependency_loader,
                        dependency_resolver=dependency_resolver,
                    )
                    if entity is None:
                        continue
                except Exception as exc:
                    log.warning(
                        "Skipping template entity %s for %s: %s",
                        template_path,
                        Entity.name,
                        exc,
                    )
                    continue
                Entity.template = entity
                Entity.templatePath = template_path
                Entities.append(Entity)
            else:
                # Entity chunk has no Template handle — occurs with inline/streamed entities
                # (e.g. CWitcherSword, Crossbow). Read from streamingDataBuffer instead.
                streaming_data_prop = chunk.GetVariableByName('streamingDataBuffer')
                if streaming_data_prop:
                    buffer_bytes = _extract_streaming_buffer_bytes(file, streaming_data_prop)
                    if buffer_bytes:
                        f = bStream(data=bytearray(buffer_bytes))
                        f.name = "DATA_BUFFER"
                        bufferedCR2W = getCR2W(f)
                        entity = create_level(
                            bufferedCR2W,
                            chunk.name,
                            dependency_loader=dependency_loader,
                            dependency_resolver=dependency_resolver,
                        )
                        Entity.streamingDataBuffer = entity
                        if Entity.name == Entity.type:
                            stream_chunk = None
                            try:
                                stream_chunk = Entity.streamingDataBuffer.CHUNKS.CHUNKS[0]
                            except Exception:
                                stream_chunk = None

                            resource_prop = stream_chunk.GetVariableByName('resource') if stream_chunk else None
                            mesh_prop = stream_chunk.GetVariableByName('mesh') if stream_chunk else None

                            resource_path = _first_handle_depot_path(resource_prop)
                            mesh_path = _first_handle_depot_path(mesh_prop)

                            if getattr(stream_chunk, "Type", None) == 'CClothComponent' and resource_path:
                                Entity.name = Path(resource_path).stem
                                Entity.name += f" ({Entity.type}) (CClothComponent)"
                            elif mesh_path:
                                Entity.name = Path(mesh_path).stem
                                Entity.name += f" ({Entity.type})"
                            else:
                                stream_label = _stream_chunk_string_prop(stream_chunk, "name", "actionName")
                                if stream_label:
                                    Entity.name = stream_label
                                    Entity.name += f" ({Entity.type})"
                                elif not _should_suppress_streaming_name_warning(
                                    stream_chunk,
                                    resource_path,
                                    mesh_path,
                                ):
                                    detail = _describe_streaming_name_failure(
                                        file,
                                        chunk,
                                        stream_chunk,
                                        resource_prop,
                                        mesh_prop,
                                    )
                                    log.warning("Streaming entity name resolution failed (%s)", detail)
                    else:
                        log.debug(
                            "Skipping inline entity buffer decode for %s in %s: no embedded streamingDataBuffer bytes.",
                            Entity.name,
                            getattr(file, "fileName", "<memory>"),
                        )
                try:
                    if hasattr(chunk, 'Components'):
                        for chunk_id in chunk.Components:
                            sub_chunk  = CHUNKS[chunk_id-1]
                            if sub_chunk.name == "CPointLightComponent":
                                Entity.Components.append(sub_chunk)
                            elif sub_chunk.name == "CSpotLightComponent":
                                Entity.Components.append(sub_chunk)
                            elif sub_chunk.name == "CMeshComponent":
                                Entity.Components.append(sub_chunk)
                            elif sub_chunk.name == "CStaticMeshComponent":
                                Entity.Components.append(sub_chunk)
                            elif sub_chunk.name == "CAreaComponent":
                                Entity.Components.append(sub_chunk)
                            elif sub_chunk.name == "CWaterComponent":
                                Entity.Components.append(sub_chunk)
                except Exception as e:
                    pass#raise e
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
