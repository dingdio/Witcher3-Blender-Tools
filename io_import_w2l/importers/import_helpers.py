import os
try:
    import bpy
except Exception as e:
    pass
from pathlib import Path

from CR2W.CR2W_helpers import Enums

#from io_import_w2l.setup_logging_bl import *
from CR2W.setup_logging import *
log = logging.getLogger(__name__)
from io_import_w2l import get_fbx_uncook_path

def get_w3_level_data(level, fbx_uncook_path = False):
    if fbx_uncook_path == False:
        fbx_uncook_path = get_fbx_uncook_path(bpy.context)
    mesh_objects = []
    mesh_paths = []

    if level.CSectorData:
        
        #import entities hold import data
        CSectorData_ENTITY = Import_Entity()
        CSectorData_ENTITY.type = "CSectorData"
        CSectorData_ENTITY.name = "CSectorData_Transform"
        #meshPath entities hold a transform and componants such as import data
        # THIS_ENTITY = meshPath("CSectorData_Transform", False, False, fbx_uncook_path, BasicEngineQsTransform())
        # THIS_ENTITY.type = "Entity"
        for idx, block in enumerate(level.CSectorData.BlockData):
            #TESTING
            this_type = Enums.BlockDataObjectType.getEnum(block.packedObjectType)
            if this_type != "mesh" and block.resourceIndex < 12:
                pass#print("cake")
            if block.resourceIndex < 12:
                this_resource = level.CSectorData.Resources[block.resourceIndex].pathHash
                print(block.resourceIndex, this_resource)

            if block.packedObjectType == Enums.BlockDataObjectType.Mesh:# or block.packedObjectType == Enums.BlockDataObjectType.Invalid:
                mesh_objects.append(block)
                mesh_path = level.CSectorData.Resources[block.packedObject.meshIndex].pathHash
                #obj_pos = level.CSectorData.Objects[idx].position
                CSectorData_ENTITY.static_mesh_list.append(meshPath(mesh_path, block.position, MatrixToArray(block.rotationMatrix), fbx_uncook_path ))
            if block.packedObjectType == Enums.BlockDataObjectType.RigidBody:
                mesh_objects.append(block)
                mesh_path = level.CSectorData.Resources[block.packedObject.meshIndex].pathHash
                CSectorData_ENTITY.static_mesh_list.append(meshPath(mesh_path, block.position, MatrixToArray(block.rotationMatrix), fbx_uncook_path ))
                log.info("found RigidBody in CSectorData")
            if block.packedObjectType == Enums.BlockDataObjectType.Collision:
                mesh_objects.append(block)
                mesh_path = level.CSectorData.Resources[block.packedObject.meshIndex].pathHash
                CSectorData_ENTITY.static_mesh_list.append(meshPath(mesh_path, block.position, MatrixToArray(block.rotationMatrix), fbx_uncook_path ))
                log.info("found Collision in CSectorData")
            if block.packedObjectType == Enums.BlockDataObjectType.PointLight:
                log.info("found point light in CSectorData")
            if block.packedObjectType == Enums.BlockDataObjectType.SpotLight:
                light_path = level.CSectorData.Resources[block.resourceIndex].pathHash
                log.info("found spot light in CSectorData")
            if block.packedObjectType == Enums.BlockDataObjectType.Invalid:
                log.info("found point Invalid in CSectorData")
            if block.packedObjectType == Enums.BlockDataObjectType.Cloth:
                log.info("found point Cloth in CSectorData")
            if block.packedObjectType == Enums.BlockDataObjectType.Decal:
                log.info("found point Decal in CSectorData")
            if block.packedObjectType == Enums.BlockDataObjectType.Destruction:
                log.info("found point Destruction in CSectorData")
            if block.packedObjectType == Enums.BlockDataObjectType.Dimmer:
                log.info("found point Dimmer in CSectorData")
            if block.packedObjectType == Enums.BlockDataObjectType.Particles:
                log.info("found point Particles in CSectorData")
        log.info("CSectorData Found")
        #THIS_ENTITY.components.append(CSectorData_ENTITY)
        mesh_paths.append(CSectorData_ENTITY)
    if level.Entities:
        mesh_paths = mesh_paths + get_Entities(level)
    w3_level_data = levelExportData(level.layerNode, mesh_paths)
    return w3_level_data

def get_Entities(level, fbx_uncook_path = False):
    if fbx_uncook_path == False:
        fbx_uncook_path = get_fbx_uncook_path(bpy.context)
    final_Entities =[]
    for ent in level.Entities:
        THIS_ENTITY = False
        if hasattr(ent, 'bufferedCR2W') and ent.bufferedCR2W:
            THIS_ENTITY = Import_Entity()
            #TODO HANDLE MORE THAN A SINGLE MESH IN THE meshPath Class
            for meshChunk in ent.bufferedCR2W.CHUNKS.CHUNKS:
                if meshChunk.name == "CStaticMeshComponent":
                    name = meshChunk.GetVariableByName('name')
                    transform = meshChunk.GetVariableByName('transform')
                    if transform:
                        EngineTransform = transform.EngineTransform
                    else:
                        EngineTransform = None
                    DepotPath = meshChunk.GetVariableByName('mesh').Handles[0].DepotPath
                    THIS_ENTITY.static_mesh_list.append(meshPath(DepotPath, False, False, fbx_uncook_path, EngineTransform ))
                if meshChunk.name == "CMeshComponent":
                    name = meshChunk.GetVariableByName('name')
                    transform = meshChunk.GetVariableByName('transform')
                    if transform:
                        EngineTransform = transform.EngineTransform
                    else:
                        EngineTransform = None
                    DepotPath = meshChunk.GetVariableByName('mesh').Handles[0].DepotPath
                    THIS_ENTITY.mesh_list.append(meshPath(DepotPath, False, False, fbx_uncook_path, EngineTransform ))
        
        if THIS_ENTITY and hasattr(ent, 'sub_chunks'):
            for sub_chunk in ent.sub_chunks:
                if sub_chunk.name == "CPointLightComponent":
                    #light = CPointLightComponent(sub_chunk)
                    THIS_ENTITY.point_light_list.append(sub_chunk)
                if sub_chunk.name == "CSpotLightComponent":
                    THIS_ENTITY.spot_light_list.append(sub_chunk)
        else:
            log.warning("Found light without mesh parent")
            #print("WARNING: Found light without mesh parent")
        if hasattr(ent, 'template') and hasattr(ent.template, 'Entities') and ent.template.Entities:
            THIS_ENTITY = Transform_Entity("Entity", False, False, fbx_uncook_path, ent.transform )
            THIS_ENTITY.components = get_Entities(ent.template)
            #THIS_ENTITY.type = "Transform_Entity"
        if THIS_ENTITY:
            final_Entities.append(THIS_ENTITY)
    return final_Entities

def get_entity_data(level, fbx_uncook_path = False):
    if fbx_uncook_path == False:
        fbx_uncook_path = get_fbx_uncook_path(bpy.context)
    mesh_objects = []
    mesh_paths = []
    if level.Entities:
        mesh_paths = get_Entities(level)
    w3_level_data = levelExportData(level.layerNode, mesh_paths)
    return w3_level_data


class CPointLightComponent:
    def __init__(self, chunk):
        self.tags = chunk.GetVariableByName('tags')      #TagList" />
        self.transform = chunk.GetVariableByName('transform')      #EngineTransform" />
        self.transformParent = chunk.GetVariableByName('transformParent')      #ptr:CHardAttachment" />
        self.guid = chunk.GetVariableByName('guid')      #CGUID" />
        self.name = chunk.GetVariableByName('name')      #String" />
        self.isStreamed = chunk.GetVariableByName('isStreamed')      #Bool" />
        self.isVisible = chunk.GetVariableByName('isVisible')      #Bool" />
        self.icon = chunk.GetVariableByName('icon')      #handle:CBitmapTexture" />
        self.isEnabled = chunk.GetVariableByName('isEnabled')      #Bool" />
        self.shadowCastingMode = chunk.GetVariableByName('shadowCastingMode')      #ELightShadowCastingMode" />
        self.shadowFadeDistance = chunk.GetVariableByName('shadowFadeDistance')      #Float" />
        self.shadowFadeRange = chunk.GetVariableByName('shadowFadeRange')      #Float" />
        self.shadowBlendFactor = chunk.GetVariableByName('shadowBlendFactor')      #Float" />
        self.radius = chunk.GetVariableByName('radius')      #Float" />
        self.brightness = chunk.GetVariableByName('brightness')      #Float" />
        self.attenuation = chunk.GetVariableByName('attenuation')      #Float" />
        self.color = chunk.GetVariableByName('color')      #Color" />
        self.envColorGroup = chunk.GetVariableByName('envColorGroup')      #EEnvColorGroup" />
        self.autoHideDistance = chunk.GetVariableByName('autoHideDistance')      #Float" />
        self.autoHideRange = chunk.GetVariableByName('autoHideRange')      #Float" />
        self.lightFlickering = chunk.GetVariableByName('lightFlickering')      #SLightFlickering" />
        self.allowDistantFade = chunk.GetVariableByName('allowDistantFade')      #Bool" />
        self.lightUsageMask = chunk.GetVariableByName('lightUsageMask')      #ELightUsageMask" />
        self.cacheStaticShadows = chunk.GetVariableByName('cacheStaticShadows')      #Bool" />
        self.dynamicShadowsFaceMask = chunk.GetVariableByName('dynamicShadowsFaceMask')      #ELightCubeSides" />

class levelExportData:
    def __init__(self, layerNode, meshes=[]):
        self.layerNode = layerNode
        self.meshes = meshes
    @classmethod
    def from_json(cls, data):
        meshes = list(map(meshPath.from_json, data["meshes"]))
        data["meshes"] = meshes
        return cls(**data)

#This is a single group. Everything in these lists should have a common parent transform
class Import_Entity():
    def __init__(self):
        self.name = "Import_Entity"
        self.type = "CGameplayEntity"
        self.static_mesh_list = []
        self.mesh_list = []
        self.point_light_list = []
        self.spot_light_list = []


#Anything that needs to be created inside Blender and is not a mesh should use this
class Transform_Entity:
    def __init__(self, meshName, translation, matrix, fbx_uncook_path = False, transform = False):
        self.meshName = meshName
        self.translation = translation
        #self.rotation = rotation
        self.matrix = matrix
        self.fbx_uncook_path = fbx_uncook_path
        self.transform = transform
        self.components = [] #lights, submesh etc.
        self.type = "Transform_Entity"
    @classmethod
    def from_json(cls, data):
        return cls(**data)

class meshPath:
    def __init__(self, meshName = False, translation = False, matrix = False, fbx_uncook_path = False, transform = False, BlockDataObjectType = Enums.BlockDataObjectType.Mesh):
        if fbx_uncook_path == False:
            fbx_uncook_path = get_fbx_uncook_path(bpy.context)
        self.name = "Mesh Item"
        self.meshName = meshName
        self.translation = translation
        #self.rotation = rotation
        self.matrix = matrix
        self.fbx_uncook_path = fbx_uncook_path
        self.transform = transform
        self.components = [] #lights, submesh etc.
        self.type = "Mesh"
        self.BlockDataObjectType = BlockDataObjectType
    def fbxPath(self):
        name = Path(os.path.join(self.fbx_uncook_path, self.meshName))
        return str(name.with_suffix('.fbx'));
    def exists(self):
        return  Path(self.fbxPath()).exists();
    def fileName(self):
        return  os.path.basename(self.fbxPath())
    def filePath(self):
        return  os.path.dirname(self.fbxPath())
    def static_from_chunk(self, meshChunk):
        #DepotPath = meshChunk.GetVariableByName('mesh').Handles[0].DepotPath
        self.meshName = meshChunk.GetVariableByName('mesh').Handles[0].DepotPath

        if meshChunk.GetVariableByName('name'):
            self.name = meshChunk.GetVariableByName('name').String.String
        if meshChunk.GetVariableByName('transform'):
            self.transform = meshChunk.GetVariableByName('transform').EngineTransform
        else:
            self.transform = None
        return self
    @classmethod
    def from_json(cls, data):
        return cls(**data)

def MatrixToArray(rm):
    r11 = rm.ax;
    r12 = rm.ay;
    r13 = rm.az;

    r21 = rm.bx;
    r22 = rm.by;
    r23 = rm.bz;

    r31 = rm.cx;
    r32 = rm.cy;
    r33 = rm.cz;
    return  [[ r11, r12, r13 ], [ r21, r22, r23 ], [ r31, r32, r33 ] ];

class BasicEngineQsTransform:
    def __init__(self):
        self.X = 0.0
        self.Y = 0.0
        self.Z = 0.0
        self.Pitch = 0.0
        self.Yaw = 0.0
        self.Roll = 0.0
        self.Scale_x = 1.0
        self.Scale_y = 1.0
        self.Scale_z = 1.0

## FOR JSON LOADING
# def loadWitcherLevel(filename):
#     dirpath, file = os.path.split(filename)
#     basename, ext = os.path.splitext(file)
#     if ext.lower() in ('.json'):
#         with open(filename) as file:
#             data = file.read()
#             jsonData = json.loads(data)
#             w3_level_data = levelExportData.from_json(jsonData)
#     else:
#         w3_level_data = None

#     return w3_level_data


def checkLevel(levelData):
    levelFile = levelData.layerNode
    errors = ['======Errors======= '+ levelFile]
    ready_to_import = True

    if levelData.CSectorData:
        pass
        

    for ENTITY_OBJECT in levelData.Entities:
        if ENTITY_OBJECT.type == "CGameplayEntity" or ENTITY_OBJECT.type == "CEntity":
            if ENTITY_OBJECT.isCreatedFromTemplate:
                for entity in ENTITY_OBJECT.template.Entities:
                    if hasattr(entity, "streamingDataBuffer") and entity.streamingDataBuffer:
                        for chunk in entity.streamingDataBuffer.CHUNKS.CHUNKS:
                            if chunk.name == "CStaticMeshComponent" or chunk.name == "CMeshComponent":
                                mesh = meshPath().static_from_chunk(chunk)
                                if mesh and mesh.exists():
                                    pass# print(mesh.exists(), mesh.fbxPath())
                                else:
                                    error_message = "ERROR LEVEL MESH MISSING: Can't import: "+mesh.fbxPath()
                                    #print(error_message)
                                    errors.append(error_message)
                                    ready_to_import = False
                                    
    if len(errors) == 1:
        errors[0] = '======GREEN======= '+ levelFile

    if ready_to_import:
        return True
    else:
        print("Missing", len(errors) ,"/", len(levelData.Entities))
        return False

