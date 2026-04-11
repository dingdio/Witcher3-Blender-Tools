
import logging
import os
import re

from .dc_entity import (
    CCollisionShapeConvex, CCollisionShapeTriMesh,
    CCollisionShapeBox, CCollisionShapeSphere, CCollisionShapeCapsule
)
from .helper_function import flip_v
from .bStream import bStream
from .dc_mesh import lin2srgb, srgb2lin
log = logging.getLogger(__name__)
import numpy as np
from collections import defaultdict

from .CR2W_types import (CR2W, CEnum,
                            CR2W_header,
                            CR2WImport,
                            CR2WExport,
                            CR2WProperty,
                            CR2WBuffer,
                            DATA,
                            W_CLASS,
                            PROPERTY,
                            HANDLE,
                            CArray,
                            CVariantSizeNameType,
                            CMaterialInstance,
                            CBufferVLQInt32, # ADD TYPE TO W_CLASS etc.
                            CPaddedBuffer,
                            CSTRING,
                            CDATETIME)

from .Types.CMesh import (CMesh)
from .Types.VariousTypes import (CUInt16,
                                     CUInt32,
                                     NAME,
                                     CNAME_INDEX,
                                     CNAME,
                                    CMatrix4x4,
                                    CFloat)

from .Types.SBufferInfos import (BoneData, EMeshVertexType)
import base64


def printProps(PROPS):
    """Debug utility: dump CR2W property tree to log. Only outputs at DEBUG level."""
    for prop in PROPS:
        try:
            if hasattr(prop, 'isUTF'):
                log.debug("STRING (String = '%s', isUTF='%s')", prop.String, prop.isUTF)
            elif hasattr(prop, 'Value'):
                log.debug("PROPERTY(Value = %s, theName='%s', theType='%s')", prop.Value, prop.theName, prop.theType)
            elif hasattr(prop, 'ValueA'):
                log.debug("PROPERTY(ValueA = %s, theName='%s', theType='%s')", prop.ValueA, prop.theName, prop.theType)
            elif hasattr(prop, 'String'):
                log.debug("PROPERTY(theName='%s', theType='%s', String = STRING(isUTF = %s, String = '%s'))", prop.theName, prop.theType, prop.String.isUTF, prop.String.String)
            elif hasattr(prop, 'DateTime'):
                log.debug("PROPERTY(theName='%s', theType='%s', DateTime = CDATETIME(Value = %s, String = '%s'))", prop.theName, prop.theType, prop.DateTime.Value, prop.DateTime.String)
            elif hasattr(prop, 'elements'):
                log.debug("ARRAY START: theName='%s', theType='%s'", prop.theName, prop.theType)
                printProps(prop.elements)
                log.debug("ARRAY END")
            elif hasattr(prop, 'MoreProps'):
                log.debug("MoreProps START")
                try:
                    log.debug("more_prop_%s = PROPERTY(theName='%s', theType='%s')", prop.theName, prop.theName, prop.theType)
                except Exception:
                    log.debug("more_prop_%s", prop.__class__.__name__)
                printProps(prop.MoreProps) if hasattr(prop, 'MoreProps') else None
                log.debug("MoreProps END")
            elif hasattr(prop, 'More'):
                log.debug("More START")
                try:
                    log.debug("more_prop_%s = PROPERTY(theName='%s', theType='%s')", prop.theName, prop.theName, prop.theType)
                except Exception:
                    log.debug("more_prop_%s", prop.__class__.__name__)
                printProps(prop.More) if hasattr(prop, 'More') else None
                log.debug("More END")
            elif hasattr(prop, 'PROPS'):
                log.debug("PROPS START")
                try:
                    log.debug("more_prop_%s = PROPERTY(theName='%s', theType='%s')", prop.theName, prop.theName, prop.theType)
                except Exception:
                    log.debug("more_prop_%s", prop.__class__.__name__)
                printProps(prop.PROPS) if hasattr(prop, 'PROPS') else None
                log.debug("PROPS END")
            else:
                log.debug("UNK PROP VAL, theName='%s', theType='%s'", prop.theName, prop.theType)

        except Exception as e:
            log.debug("FAILED to print prop: %s", e)

    return None


def print_CR2W(PROPS):
    log.debug("================ START OF PROP LIST =================")
    printProps(PROPS)
    log.debug("================ END OF PROP LIST ===================")


def Build_Meshbuffer():
    pass

def Build_CMesh_Chunk(cr2w, ALL_LODS, bone_data:BoneData = None, common_info = None, MATERIAL_DICT= None):
    CHUNK_INDEX = cr2w.HEADER.numChunks
    cr2w.HEADER.numChunks += 1
    
    ## CREATE EXPORT
    cr2w.CR2WExport.append(CR2WExport(
                            crc32=0,
                            dataOffset=0,
                            dataSize=0,
                            name='CMesh',
                            objectFlags=0,
                            parentID=0,
                            template=0))
    # CREATE ACTUAL W_CLASS
    _CMesh = CMesh(cr2w)
    _CMesh_chunk = W_CLASS(
                        CR2WFILE = cr2w,
                        idx = CHUNK_INDEX,
                        PROPS= [],
                        Type='CMesh',
                        name='CMesh')
    _CMesh_chunk.CMesh = _CMesh

    def add_importFile():
        _CMesh_chunk.PROPS.append(PROPERTY(theName='importFile', theType='String', String = CSTRING(isUTF = False, String = 'D:\Dev\box.hkx')))

    def add_importFileTimeStamp():
        _CMesh_chunk.PROPS.append(PROPERTY(theName='importFileTimeStamp', theType='CDateTime', DateTime = CDATETIME(Value = 247518305951179776, String = '2010/01/26 13:47:23')))  

    def add_materialNames(material_dict):
        array = PROPERTY( theName='materialNames', theType='array:2,0,String', elements = [])
        for mat_data in material_dict.values():
            array.elements.append(CSTRING(String = mat_data['name'], isUTF='False'))
        _CMesh_chunk.PROPS.append(array)

    def add_authorName():
        _CMesh_chunk.PROPS.append(PROPERTY(theName='authorName', theType='String', String = CSTRING(isUTF = False, String = 'No Author Name')))
            
    def add_materials(material_dict):
        #TODO material type ChunkHandle or external
        #TODO ensure same number of mateiral names
        #### CHUNK HANDLE EXAMPLE
        handles = []
        local_chunk_idx = 1
        for idx, mat_data in enumerate(material_dict.values()):
            witcher_props = mat_data['witcher_props']
            if witcher_props['local']:
                handles.append(HANDLE(CR2WFILE=cr2w,
                                ChunkHandle= True,
                                ClassName=None,
                                DepotPath=None,
                                Flags=None,
                                Index=None,
                                Reference = local_chunk_idx,
                                theType='handle:IMaterial',
                                val = local_chunk_idx))
                local_chunk_idx +=1
            else:
                depo = witcher_props['base_custom'] #if witcher_props['base'] == 'custom' else witcher_props['base']
                handles.append(HANDLE(CR2WFILE=cr2w,
                                ChunkHandle = False,
                                ClassName = 'CMaterialInstance' if depo.endswith('.w2mi') else'CMaterialGraph',
                                DepotPath = depo, #'engine\\materials\\graphs\\pbr_std.w2mg',
                                Flags = 0,
                                Index = None,
                                Reference = None,
                                theType = 'handle:IMaterial',
                                val= -1))
        new_handle_prop = PROPERTY(CR2WFILE=cr2w,
                                    Handles = handles,
                                    elements = handles,
                                    Value = 2,
                                    theName = 'materials',
                                    theType = 'array:2,0,handle:IMaterial',)
        _CMesh_chunk.PROPS.append(new_handle_prop)

    def add_boundingBox(common_info):
        min = common_info['boundingBox'][0]
        max = common_info['boundingBox'][1]
        more_prop_boundingBox = PROPERTY( theName='boundingBox', theType='Box', More=[])
        more_prop_Min = PROPERTY( theName='Min', theType='Vector', More=[
            PROPERTY(Value = min[0], theName='X', theType='Float'),
            PROPERTY(Value = min[1], theName='Y', theType='Float'),
            PROPERTY(Value = min[2], theName='Z', theType='Float'),
            PROPERTY(Value = 1.0, theName='W', theType='Float')
        ])
        more_prop_Max = PROPERTY( theName='Max', theType='Vector', More=[
            PROPERTY(Value = max[0], theName='X', theType='Float'),
            PROPERTY(Value = max[1], theName='Y', theType='Float'),
            PROPERTY(Value = max[2], theName='Z', theType='Float'),
            PROPERTY(Value = 1.0, theName='W', theType='Float')
        ])
        more_prop_boundingBox.More = [
            more_prop_Min,
            more_prop_Max
        ]
        _CMesh_chunk.PROPS.append(more_prop_boundingBox)

    def add_autoHideDistance(common_info):
        _CMesh_chunk.PROPS.append(PROPERTY(Value = float(common_info['lod0_MeshSettings'].autohideDistance), theName='autoHideDistance', theType='Float'))

    def add_isTwoSided(common_info):
        if common_info['lod0_MeshSettings'].isTwoSided:
            _CMesh_chunk.PROPS.append(PROPERTY(Value = True, theName='isTwoSided', theType='Bool'))

    #This seems to determine if UV2 and vertex color is added to the cook
    def add_useExtraStreams(common_info):
        if common_info['lod0_MeshSettings'].useExtraStreams:
            _CMesh_chunk.PROPS.append(PROPERTY(Value = True, theName='useExtraStreams', theType='Bool'))

    def add_generalizedMeshRadius(common_info):
        _CMesh_chunk.PROPS.append(PROPERTY(Value = common_info['generalizedMeshRadius'], theName='generalizedMeshRadius', theType='Float'))

    def add_mergeInGlobalShadowMesh(common_info):
        if not common_info['lod0_MeshSettings'].mergeInGlobalShadowMesh:
            _CMesh_chunk.PROPS.append(PROPERTY(Value = False, theName='mergeInGlobalShadowMesh', theType='Bool'))

    def add_isOccluder(common_info):
        if not common_info['lod0_MeshSettings'].isOccluder:
            _CMesh_chunk.PROPS.append(PROPERTY(Value = False, theName='isOccluder', theType='Bool'))

    def add_smallestHoleOverride(common_info):
        val = common_info['lod0_MeshSettings'].smallestHoleOverride
        if val != -1.0:
            _CMesh_chunk.PROPS.append(PROPERTY(Value = float(val), theName='smallestHoleOverride', theType='Float'))

    def add_chunks(*args):

        array = PROPERTY( theName='chunks', theType='array:2,0,SMeshChunkPacked', elements = [])
        for arg in args:
            (vertex_type,
            materialID,
            numBonesPerVertex,
            numVertices,
            numIndices,
            firstVertex,
            firstIndex,
            renderMask,
            useForShadowmesh) = arg
            more_prop_SMeshChunkPacked = PROPERTY( theName='SMeshChunkPacked', theType='SMeshChunkPacked', MoreProps=[
                PROPERTY(Value = numVertices, theName='numVertices', theType='Uint32'),
                PROPERTY(Value = numIndices, theName='numIndices', theType='Uint32'),
            ])
            
            if firstVertex:
                more_prop_SMeshChunkPacked.MoreProps.append(PROPERTY(Value = firstVertex, theName='firstVertex', theType='Uint32'))
            
            if firstIndex:
                more_prop_SMeshChunkPacked.MoreProps.append(PROPERTY(Value = firstIndex, theName='firstIndex', theType='Uint32'))

            # Always write explicit shadow-mesh usage so the chunk data is unambiguous.
            more_prop_SMeshChunkPacked.MoreProps.append(PROPERTY(Value = bool(useForShadowmesh), theName='useForShadowmesh', theType='Bool'))

            if materialID:
                more_prop_SMeshChunkPacked.MoreProps.append(PROPERTY(Value = materialID, theName='materialID', theType='Uint32'))

            if vertex_type == EMeshVertexType.EMVT_SKINNED:
                vertexType = CEnum(cr2w)
                vertexType.String = 'MVT_SkinnedMesh'
                vertexType.strings = ['MVT_SkinnedMesh']
                more_prop_SMeshChunkPacked.MoreProps.append(PROPERTY(Value = numBonesPerVertex, theName='numBonesPerVertex', theType='Uint8'))
                more_prop_SMeshChunkPacked.MoreProps.append(PROPERTY(Value = vertex_type, theName='vertexType', theType='EMeshVertexType', Index = vertexType ))

            array.elements.append(more_prop_SMeshChunkPacked)
        _CMesh_chunk.PROPS.append(array)

    def add_rawVertices():
        _CMesh_chunk.PROPS.append(PROPERTY(ValueA = 1, theName='rawVertices', theType='DeferredDataBuffer'))

    def add_rawIndices():
        _CMesh_chunk.PROPS.append(PROPERTY(ValueA = 2, theName='rawIndices', theType='DeferredDataBuffer'))

    def add_isStatic(common_info):
        # Derived from the export context so it always matches chunk vertex types.
        if common_info['isStatic']:
            _CMesh_chunk.PROPS.append(PROPERTY(Value = True, theName='isStatic', theType='Bool'))

    def add_entityProxy(common_info):
        if common_info['lod0_MeshSettings'].entityProxy:
            _CMesh_chunk.PROPS.append(PROPERTY(Value = True, theName='entityProxy', theType='Bool'))

    def add_cookedData():
        more_prop_cookedData = PROPERTY( theName='cookedData', theType='SMeshCookedData', More=[
            PROPERTY(ValueA = 0, theName='renderBuffer', theType='DeferredDataBuffer')
        ])
        _CMesh_chunk.PROPS.append(more_prop_cookedData)

    def add_soundInfo():
        pass

    def add_internalVersion():
        _CMesh_chunk.PROPS.append(PROPERTY(Value = 2, theName='internalVersion', theType='Uint8'))
    
    def add_chunksBuffer():
        _CMesh_chunk.PROPS.append(PROPERTY(ValueA = 0, theName='chunksBuffer', theType='DeferredDataBuffer'))

    def add_ChunkgroupIndeces(ALL_LODS):
        idx = 0
        for mesh_data in ALL_LODS:
            mesh_settings = mesh_data[1]
            mesh_data = mesh_data[0]
            p_buf = CPaddedBuffer(cr2w, CUInt16)
            num_els = len(mesh_data)
            elements = []
            for _ in range(num_els):
                element = p_buf.buffer_type(p_buf.CR2WFILE)
                element.val = idx
                idx += 1
                elements.append(element)
            p_buf.AddElements(elements, 0)
            p_buf.padding = mesh_settings.distance
            _CMesh_chunk.CMesh.ChunkgroupIndeces.elements.append(p_buf)

    def add_BoneNames(jointNames):
        for joint in jointNames:
            name_obj = NAME( name = joint )
            _CMesh_chunk.CMesh.BoneNames.elements.append(CNAME_INDEX(cr2w, value = name_obj ))

    def add_Bonematrices(bonematrices):
        for dis in bonematrices:
            _CMesh_chunk.CMesh.Bonematrices.elements.append(dis)

    def add_Block3(block3):
        for dis in block3:
            _CMesh_chunk.CMesh.Block3.elements.append(CFloat(cr2w, val = dis ))


    def add_BoneIndecesMappingBoneIndex(boneIndecesMappingBoneIndex):
        for mapping in boneIndecesMappingBoneIndex:
            _CMesh_chunk.CMesh.BoneIndecesMappingBoneIndex.elements.append(CUInt32(cr2w, val = mapping ))


    add_importFile() if False else None
    add_importFileTimeStamp() if False else None
    add_authorName() if False else None

    add_materialNames(MATERIAL_DICT)

    add_materials(MATERIAL_DICT
            #{'ChunkHandle':True, # internal chunk handle
            # 'val':1}, # The ref chunk index
            # {'ChunkHandle': False, # direct material graphc ref
            # 'ClassName': 'CMaterialGraph',
            # 'DepotPath': 'engine\\materials\\graphs\\pbr_std.w2mg'},
            
            # {'ChunkHandle': False, # material instance
            # 'ClassName': 'CMaterialInstance',
            # 'DepotPath': r'environment\textures_tileable\common_materials\elven_ruins\elven_guard_fresco.w2mi'}
            )
    add_boundingBox(common_info) if True else None
    add_autoHideDistance(common_info) if True else None
    add_isTwoSided(common_info) if True else None
    add_useExtraStreams(common_info) if True else None
    add_generalizedMeshRadius(common_info) if True else None
    add_mergeInGlobalShadowMesh(common_info) if True else None
    add_isOccluder(common_info) if True else None
    add_smallestHoleOverride(common_info) if True else None
    
    
    def _resolve_lod_level(mesh_settings, fallback_idx):
        level = getattr(mesh_settings, "lod_level", None)
        try:
            return int(level)
        except (TypeError, ValueError):
            return fallback_idx

    lod_levels = [_resolve_lod_level(lod_entry[1], idx) for idx, lod_entry in enumerate(ALL_LODS)]
    shadow_lod_level = max(lod_levels) if lod_levels else 0

    # Populate firstVertex/firstIndex before building chunk metadata so the
    # chunk table references the correct ranges for every submesh.
    running_first_vertex = 0
    running_first_index = 0
    for lod_entry in ALL_LODS:
        lod_meshes = lod_entry[0]
        for bl_mesh_info, _ in lod_meshes:
            bl_mesh_info.meshInfo.firstVertex = running_first_vertex
            bl_mesh_info.meshInfo.firstIndex = running_first_index
            running_first_vertex += bl_mesh_info.meshInfo.numVertices
            running_first_index += bl_mesh_info.meshInfo.numIndices

    chunks_to_make = []
    for lod_idx, mesh_data in enumerate(ALL_LODS):
        mesh_settings = mesh_data[1]
        lod_level = _resolve_lod_level(mesh_settings, lod_idx)
        mesh_data = mesh_data[0]
        for idx, (bl_mesh_info, witcher_mat_info) in enumerate(mesh_data):
            vertex_type = EMeshVertexType.EMVT_STATIC if common_info['isStatic'] else EMeshVertexType.EMVT_SKINNED #EMeshVertexType
            mat_name = witcher_mat_info[0]['name'] if witcher_mat_info else 'Material0'
            materialID = list(MATERIAL_DICT.keys()).index(mat_name)
            numBonesPerVertex = 4 #Uint8 0
            numVertices = bl_mesh_info.meshInfo.numVertices #Uint32
            numIndices = bl_mesh_info.meshInfo.numIndices #Uint32
            firstVertex = bl_mesh_info.meshInfo.firstVertex # Uint32 0
            firstIndex = bl_mesh_info.meshInfo.firstIndex # Uint32 0
            renderMask = 0 # EMeshChunkRenderMask
            useForShadowmesh = (lod_level == shadow_lod_level) # use lowest detail LOD for shadows
            
            chunks_to_make.append([
                    vertex_type,
                    materialID,
                    numBonesPerVertex,
                    numVertices,
                    numIndices,
                    firstVertex,
                    firstIndex,
                    renderMask,
                    useForShadowmesh,
                    ])
    add_chunks(*chunks_to_make)#[24, 36, 1]),[24, 36, 1]) #[numVertices, numIndices, useForShadowmesh]
    add_rawVertices() if True else None
    add_rawIndices() if True else None
    add_isStatic(common_info) if True else None
    add_entityProxy(common_info) if True else None
    add_cookedData() if True else None
    add_soundInfo() if True else None
    add_internalVersion() if True else None
    add_chunksBuffer() if True else None

    #arrays START
    add_ChunkgroupIndeces(ALL_LODS) if True else None
    add_BoneNames(bone_data.jointNames) if bone_data.jointNames else None
    add_Bonematrices(bone_data.boneMatrices) if bone_data.boneMatrices else None
    add_Block3(bone_data.Block3) if bone_data.Block3 else None
    add_BoneIndecesMappingBoneIndex(bone_data.BoneIndecesMappingBoneIndex) if bone_data.BoneIndecesMappingBoneIndex else None

    cr2w.CHUNKS.CHUNKS.append(_CMesh_chunk)
    
def Build_CMaterialInstance_Chunk(cr2w, inst):
    CHUNK_INDEX = cr2w.HEADER.numChunks
    cr2w.HEADER.numChunks += 1
    cr2w.CR2WExport.append(CR2WExport(
                            crc32=0,
                            dataOffset=0,
                            dataSize=0,
                            name='CMaterialInstance',
                            objectFlags=0,
                            parentID=1, # chunk index + 1
                            template=0))

    cMaterialInstance = CMaterialInstance(cr2w)
    _CMaterialInstance_holder = W_CLASS(
                        CR2WFILE = cr2w,
                        idx = CHUNK_INDEX, #1,
                        CMaterialInstance = cMaterialInstance,#<CR2W.CR2W_types.CArray>
                        PROPS = [], #[]
                        Type = 'CMaterialInstance',
                        #classEnd = 0,
                        name = 'CMaterialInstance',
                        #propCount = 0
                        )

    def add_baseMaterial(DepotPath ='engine\\materials\\graphs\\pbr_std.w2mg', ClassName = 'CMaterialGraph'):
        #?File in Repo Handle
        handle = HANDLE(CR2WFILE=cr2w,
                        ChunkHandle = False,
                        ClassName = ClassName,
                        DepotPath = DepotPath,
                        Flags = 0,
                        Index = None,
                        Reference = None,
                        theType = 'handle:IMaterial',
                        val= -1)
        new_handle_prop = PROPERTY(CR2WFILE=cr2w,
                                    Handles = [handle],
                                    theName = 'baseMaterial',
                                    theType = 'handle:IMaterial')
        _CMaterialInstance_holder.PROPS.append(new_handle_prop)

    def add_enableMask():
        _CMaterialInstance_holder.PROPS.append(PROPERTY(Value = True, theName='enableMask', theType='Bool'))

    def add_InstanceParameters(input_props):
        cMaterialInstance.InstanceParameters = CArray(cr2w, CVariantSizeNameType) # CREATE ARRAY
        
        for param in input_props:
            if param['type'] == 'TEX_IMAGE' or  param['type'] == "handle:CTextureArray":
                _className = 'CBitmapTexture'
                _theType = 'handle:ITexture'
                _texPath = param['value'].rsplit('.', 1)[0] + '.xbm'
                if param['type'] == "handle:CTextureArray" or '.texarray.' in param['value']:
                    _className = 'CTextureArray'
                    _theType = 'handle:CTextureArray'
                    _texPath = re.sub(r'(.texarray).*', r'\1', param['value'])
                
                new_param = CVariantSizeNameType(cr2w)
                handle = HANDLE(CR2WFILE=cr2w,
                                ChunkHandle = False,
                                ClassName = _className, #'CBitmapTexture',
                                DepotPath = _texPath,#param['value'].replace('.tga', '.xbm'),
                                Flags = 0,
                                Index = None,
                                Reference = None,
                                theType = _theType, #'handle:Texture',
                                val = -2)
                new_param.PROP = PROPERTY(CR2WFILE=cr2w,
                                            Handles = [handle],
                                            theName=param['name'],
                                            theType= _theType)#'handle:ITexture')
                cMaterialInstance.InstanceParameters.elements.append(new_param)

            elif param['type'] == 'RGB':
                (R,G,B,A) = param['value'].split(' ; ')
                (R,G,B,A) = float(R)*255,float(G)*255,float(B)*255,float(A)*255,
                
                cMaterialInstance.InstanceParameters.elements.append(CVariantSizeNameType(cr2w, PROPERTY(theName=param['name'], theType='Color', More = [
                        PROPERTY(Value = R, theName='Red', theType='Uint8'),
                        PROPERTY(Value = G, theName='Green', theType='Uint8'),
                        PROPERTY(Value = B, theName='Blue', theType='Uint8'),
                        PROPERTY(Value = A, theName='Alpha', theType='Uint8')
                    ])))
            elif param['type'] == 'COMBXYZ':
                if len(param['value']) == 3:
                    W = 1.0
                    [X,Y,Z] = param['value']
                else:
                    [X,Y,Z,W] = param['value']
                
                cMaterialInstance.InstanceParameters.elements.append(CVariantSizeNameType(cr2w, PROPERTY(theName=param['name'], theType='Vector', More = [
                        PROPERTY(Value = X, theName='X', theType='Float'),
                        PROPERTY(Value = Y, theName='Y', theType='Float'),
                        PROPERTY(Value = Z, theName='Z', theType='Float'),
                        PROPERTY(Value = W, theName='W', theType='Float')
                    ])))
            elif param['type'] == 'VALUE':
                cMaterialInstance.InstanceParameters.elements.append(CVariantSizeNameType(cr2w, PROPERTY(Value = param['value'], theName=param['name'], theType='Float')))
            else:
                continue
                cMaterialInstance.InstanceParameters.elements.append(CVariantSizeNameType(cr2w, PROPERTY(theName='VarianceColor', theType='Color', More = [
                        PROPERTY(Value = 2, theName='Red', theType='Uint8'),
                        PROPERTY(Value = 57, theName='Green', theType='Uint8'),
                        PROPERTY(Value = 7, theName='Blue', theType='Uint8'),
                        PROPERTY(Value = 255, theName='Alpha', theType='Uint8')
                    ])))
                cMaterialInstance.InstanceParameters.elements.append(CVariantSizeNameType(cr2w, PROPERTY(Value = 50.0, theName='VarianceOffset', theType='Float')))

    base_path = inst['witcher_props']['base_custom'] #if inst['witcher_props']['base'] else inst['witcher_props']['base']

    add_baseMaterial(base_path,'CMaterialGraph' if base_path.endswith('.w2mg') else 'CMaterialInstance')
    add_enableMask() if inst['witcher_props']['enableMask'] else False
    add_InstanceParameters(inst['witcher_props']['input_props'])

    ## ADD CHUNK TO CHUNK LIST
    cr2w.CHUNKS.CHUNKS.append(_CMaterialInstance_holder)

def remove_lowest_strength(lst):
    while len(lst) > 4:
        lowest_strength = min(lst, key=lambda x: x.strength)
        lst.remove(lowest_strength)

def Build_CCollisionShapeConvex_Chunk(cr2w, shape_data, parentID):
    CHUNK_INDEX = cr2w.HEADER.numChunks
    cr2w.HEADER.numChunks += 1
    cr2w.CR2WExport.append(CR2WExport(
        crc32=0,
        dataOffset=0,
        dataSize=0,
        name='CCollisionShapeConvex',
        objectFlags=0,
        parentID=parentID,
        template=0))
    
    _CCollisionShapeConvex = W_CLASS(
        CR2WFILE=cr2w,
        idx=CHUNK_INDEX,
        PROPS=[],
        Type='CCollisionShapeConvex',
        name='CCollisionShapeConvex')
    
    # Add physicalMaterialName
    _CCollisionShapeConvex.PROPS.append(PROPERTY(
        theName='physicalMaterialName',
        theType='CName',
        String=CSTRING(isUTF=False, String=shape_data.physicalMaterialName)))
    
    # Add vertices
    vertices_array = PROPERTY(theName='vertices', theType='array:94,0,Vector', elements=[])
    for vert in shape_data.vertices:
        vector = PROPERTY(theName='Vector', theType='Vector', More=[
            PROPERTY(Value=vert[0], theName='X', theType='Float'),
            PROPERTY(Value=vert[1], theName='Y', theType='Float'),
            PROPERTY(Value=vert[2], theName='Z', theType='Float'),
            PROPERTY(Value=vert[3], theName='W', theType='Float')
        ])
        vertices_array.elements.append(vector)
    _CCollisionShapeConvex.PROPS.append(vertices_array)
    
    # Add polygons
    polygons_array = PROPERTY(theName='polygons', theType='array:94,0,Uint16', elements=[])
    for poly in shape_data.polygons:
        polygons_array.elements.append(PROPERTY(Value=poly, theName='Uint16', theType='Uint16'))
    _CCollisionShapeConvex.PROPS.append(polygons_array)
    
    cr2w.CHUNKS.CHUNKS.append(_CCollisionShapeConvex)
    return CHUNK_INDEX

def Build_CCollisionShapeTriMesh_Chunk(cr2w, shape_data, parentID):
    CHUNK_INDEX = cr2w.HEADER.numChunks
    cr2w.HEADER.numChunks += 1
    cr2w.CR2WExport.append(CR2WExport(
        crc32=0,
        dataOffset=0,
        dataSize=0,
        name='CCollisionShapeTriMesh',
        objectFlags=0,
        parentID=parentID,
        template=0))
    
    _CCollisionShapeTriMesh = W_CLASS(
        CR2WFILE=cr2w,
        idx=CHUNK_INDEX,
        PROPS=[],
        Type='CCollisionShapeTriMesh',
        name='CCollisionShapeTriMesh')
    
    # Add physicalMaterialNames
    # REDkit-authored collision trimesh files commonly serialize this as array:99,0,CName.
    # Keep import support broad, but export this canonical form for validity/compatibility.
    material_names_array = PROPERTY(theName='physicalMaterialNames', theType='array:99,0,CName', elements=[])
    for mat_name in shape_data.physicalMaterialNames:
        material_names_array.elements.append(PROPERTY(
            theName='CName',
            theType='CName',
            String=CSTRING(isUTF=False, String=mat_name)))
    _CCollisionShapeTriMesh.PROPS.append(material_names_array)
    
    # Add vertices
    vertices_array = PROPERTY(theName='vertices', theType='array:99,0,Vector', elements=[])
    for vert in shape_data.vertices:
        vector = PROPERTY(theName='Vector', theType='Vector', More=[
            PROPERTY(Value=vert[0], theName='X', theType='Float'),
            PROPERTY(Value=vert[1], theName='Y', theType='Float'),
            PROPERTY(Value=vert[2], theName='Z', theType='Float'),
            PROPERTY(Value=vert[3], theName='W', theType='Float')
        ])
        vertices_array.elements.append(vector)
    _CCollisionShapeTriMesh.PROPS.append(vertices_array)
    
    # Add triangles
    triangles_array = PROPERTY(theName='triangles', theType='array:99,0,Uint16', elements=[])
    for tri in shape_data.triangles:
        triangles_array.elements.append(PROPERTY(Value=tri, theName='Uint16', theType='Uint16'))
    _CCollisionShapeTriMesh.PROPS.append(triangles_array)
    
    # Add physicalMaterialIndexes
    indexes_array = PROPERTY(theName='physicalMaterialIndexes', theType='array:99,0,Uint16', elements=[])
    for idx in shape_data.physicalMaterialIndexes:
        indexes_array.elements.append(PROPERTY(Value=idx, theName='Uint16', theType='Uint16'))
    _CCollisionShapeTriMesh.PROPS.append(indexes_array)
    
    cr2w.CHUNKS.CHUNKS.append(_CCollisionShapeTriMesh)
    return CHUNK_INDEX

def _build_pose_matrix_property(matrix_4x4):
    """Build a pose Matrix PROPERTY from a 4x4 list of floats.

    Args:
        matrix_4x4: A 4x4 list where rows are [X, Y, Z, Translation] vectors,
                    each containing [X, Y, Z, W] components.

    Returns:
        PROPERTY: A Matrix property with nested Vector rows.
    """
    row_names = ['X', 'Y', 'Z', 'W']  # Game uses 'W' for translation row
    rows = []
    for i, row_name in enumerate(row_names):
        row = PROPERTY(theName=row_name, theType='Vector', More=[
            PROPERTY(Value=matrix_4x4[i][0], theName='X', theType='Float'),
            PROPERTY(Value=matrix_4x4[i][1], theName='Y', theType='Float'),
            PROPERTY(Value=matrix_4x4[i][2], theName='Z', theType='Float'),
            PROPERTY(Value=matrix_4x4[i][3], theName='W', theType='Float')
        ])
        rows.append(row)
    return PROPERTY(theName='pose', theType='Matrix', More=rows)

def Build_CCollisionShapeBox_Chunk(cr2w, shape_data, parentID):
    CHUNK_INDEX = cr2w.HEADER.numChunks
    cr2w.HEADER.numChunks += 1
    cr2w.CR2WExport.append(CR2WExport(
        crc32=0,
        dataOffset=0,
        dataSize=0,
        name='CCollisionShapeBox',
        objectFlags=0,
        parentID=parentID,
        template=0))

    _CCollisionShapeBox = W_CLASS(
        CR2WFILE=cr2w,
        idx=CHUNK_INDEX,
        PROPS=[],
        Type='CCollisionShapeBox',
        name='CCollisionShapeBox')

    # Add physicalMaterialName
    _CCollisionShapeBox.PROPS.append(PROPERTY(
        theName='physicalMaterialName',
        theType='CName',
        String=CSTRING(isUTF=False, String=shape_data.physicalMaterialName)))

    # Add pose matrix
    _CCollisionShapeBox.PROPS.append(_build_pose_matrix_property(shape_data.matrix_world))

    # Add halfExtends
    _CCollisionShapeBox.PROPS.append(PROPERTY(
        Value=shape_data.halfExtendsX, theName='halfExtendsX', theType='Float'))
    _CCollisionShapeBox.PROPS.append(PROPERTY(
        Value=shape_data.halfExtendsY, theName='halfExtendsY', theType='Float'))
    _CCollisionShapeBox.PROPS.append(PROPERTY(
        Value=shape_data.halfExtendsZ, theName='halfExtendsZ', theType='Float'))

    cr2w.CHUNKS.CHUNKS.append(_CCollisionShapeBox)
    return CHUNK_INDEX

def Build_CCollisionShapeSphere_Chunk(cr2w, shape_data, parentID):
    CHUNK_INDEX = cr2w.HEADER.numChunks
    cr2w.HEADER.numChunks += 1
    cr2w.CR2WExport.append(CR2WExport(
        crc32=0,
        dataOffset=0,
        dataSize=0,
        name='CCollisionShapeSphere',
        objectFlags=0,
        parentID=parentID,
        template=0))

    _CCollisionShapeSphere = W_CLASS(
        CR2WFILE=cr2w,
        idx=CHUNK_INDEX,
        PROPS=[],
        Type='CCollisionShapeSphere',
        name='CCollisionShapeSphere')

    # Add physicalMaterialName
    _CCollisionShapeSphere.PROPS.append(PROPERTY(
        theName='physicalMaterialName',
        theType='CName',
        String=CSTRING(isUTF=False, String=shape_data.physicalMaterialName)))

    # Add pose matrix
    _CCollisionShapeSphere.PROPS.append(_build_pose_matrix_property(shape_data.matrix_world))

    # Add radius
    _CCollisionShapeSphere.PROPS.append(PROPERTY(
        Value=shape_data.radius, theName='radius', theType='Float'))

    cr2w.CHUNKS.CHUNKS.append(_CCollisionShapeSphere)
    return CHUNK_INDEX

def Build_CCollisionShapeCapsule_Chunk(cr2w, shape_data, parentID):
    CHUNK_INDEX = cr2w.HEADER.numChunks
    cr2w.HEADER.numChunks += 1
    cr2w.CR2WExport.append(CR2WExport(
        crc32=0,
        dataOffset=0,
        dataSize=0,
        name='CCollisionShapeCapsule',
        objectFlags=0,
        parentID=parentID,
        template=0))

    _CCollisionShapeCapsule = W_CLASS(
        CR2WFILE=cr2w,
        idx=CHUNK_INDEX,
        PROPS=[],
        Type='CCollisionShapeCapsule',
        name='CCollisionShapeCapsule')

    # Add physicalMaterialName
    _CCollisionShapeCapsule.PROPS.append(PROPERTY(
        theName='physicalMaterialName',
        theType='CName',
        String=CSTRING(isUTF=False, String=shape_data.physicalMaterialName)))

    # Add pose matrix
    _CCollisionShapeCapsule.PROPS.append(_build_pose_matrix_property(shape_data.matrix_world))

    # Add radius
    _CCollisionShapeCapsule.PROPS.append(PROPERTY(
        Value=shape_data.radius, theName='radius', theType='Float'))

    # Add height
    _CCollisionShapeCapsule.PROPS.append(PROPERTY(
        Value=shape_data.height, theName='height', theType='Float'))

    cr2w.CHUNKS.CHUNKS.append(_CCollisionShapeCapsule)
    return CHUNK_INDEX

def Build_CCollisionMesh_Chunk(cr2w, shape_chunk_indices, parentID):
    CHUNK_INDEX = cr2w.HEADER.numChunks
    cr2w.HEADER.numChunks += 1
    cr2w.CR2WExport.append(CR2WExport(
        crc32=0,
        dataOffset=0,
        dataSize=0,
        name='CCollisionMesh',
        objectFlags=0,
        parentID=parentID,
        template=0))
    
    _CCollisionMesh = W_CLASS(
        CR2WFILE=cr2w,
        idx=CHUNK_INDEX,
        PROPS=[],
        Type='CCollisionMesh',
        name='CCollisionMesh')
    
    # Add shapes array
    shapes_array = PROPERTY(theName='shapes', theType='array:2,0,ptr:ICollisionShape', elements=[])
    for shape_idx in shape_chunk_indices:
        handle = HANDLE(
            CR2WFILE=cr2w,
            ChunkHandle=True,
            ClassName=None,
            DepotPath=None,
            Flags=None,
            Index=None,
            Reference=shape_idx,  # 0-based chunk index
            theType='ptr:ICollisionShape',
            val=shape_idx)
        shapes_array.elements.append(handle)
    _CCollisionMesh.PROPS.append(shapes_array)
    
    cr2w.CHUNKS.CHUNKS.append(_CCollisionMesh)
    return CHUNK_INDEX

def _register_child_link(cr2w, parent_idx, child_idx):
    if child_idx is None:
        return
    if not hasattr(cr2w, 'childrendict'):
        cr2w.childrendict = {}
    if parent_idx not in cr2w.childrendict:
        cr2w.childrendict[parent_idx] = []
    if child_idx not in cr2w.childrendict[parent_idx]:
        cr2w.childrendict[parent_idx].append(child_idx)

def _chunk_type_name(chunk):
    return getattr(chunk, 'Type', None) or getattr(chunk, 'name', None) or chunk.__class__.__name__

def _assert_chunk_type(cr2w, chunk_index, expected_types, context):
    chunks = getattr(getattr(cr2w, 'CHUNKS', None), 'CHUNKS', None) or []
    if chunk_index is None or chunk_index < 0 or chunk_index >= len(chunks):
        raise ValueError(f"{context}: invalid chunk reference index {chunk_index}")
    actual_type = _chunk_type_name(chunks[chunk_index])
    if actual_type not in expected_types:
        expected_str = ', '.join(sorted(expected_types))
        raise ValueError(
            f"{context}: expected [{expected_str}] but got [{actual_type}] at chunk index {chunk_index}"
        )

def _build_chunk_handle(chunk_index, handle_type):
    # Build a chunk handle without HANDLE.Create side effects on childrendict.
    handle = HANDLE.__new__(HANDLE)
    handle.ChunkHandle = True
    handle.Reference = chunk_index
    handle.val = chunk_index + 1
    handle.DepotPath = None
    handle.ClassName = None
    handle.Flags = None
    handle.theType = handle_type
    handle.Index = None
    return handle

def _upsert_cmesh_handle_prop(cmesh_chunk, prop_name, prop_type, chunk_index):
    insert_idx = len(cmesh_chunk.PROPS)
    for i, prop in enumerate(cmesh_chunk.PROPS):
        if getattr(prop, 'theName', None) == 'internalVersion':
            insert_idx = i
            break

    for i, prop in enumerate(cmesh_chunk.PROPS):
        if getattr(prop, 'theName', None) == prop_name:
            cmesh_chunk.PROPS.pop(i)
            if i < insert_idx:
                insert_idx -= 1
            break

    handle = _build_chunk_handle(chunk_index, prop_type)
    cmesh_chunk.PROPS.insert(insert_idx, PROPERTY(
        theName=prop_name,
        theType=prop_type,
        Value=chunk_index + 1,
        Handles=[handle],
    ))

def _validate_mesh_chunk_links(cr2w):
    chunks = getattr(getattr(cr2w, 'CHUNKS', None), 'CHUNKS', None) or []
    if not chunks:
        return

    cmesh_chunk = chunks[0]
    for prop in getattr(cmesh_chunk, 'PROPS', []) or []:
        if prop.theName == 'collisionMesh':
            handles = getattr(prop, 'Handles', None) or getattr(prop, 'elements', None) or []
            if not handles:
                raise ValueError("CMesh.collisionMesh: missing handle")
            _assert_chunk_type(cr2w, handles[0].Reference, {'CCollisionMesh'}, 'CMesh.collisionMesh')
        elif prop.theName == 'soundInfo':
            handles = getattr(prop, 'Handles', None) or getattr(prop, 'elements', None) or []
            if not handles:
                raise ValueError("CMesh.soundInfo: missing handle")
            _assert_chunk_type(cr2w, handles[0].Reference, {'SMeshSoundInfo'}, 'CMesh.soundInfo')

    shape_types = {
        'CCollisionShapeConvex',
        'CCollisionShapeTriMesh',
        'CCollisionShapeBox',
        'CCollisionShapeSphere',
        'CCollisionShapeCapsule',
    }
    for chunk in chunks:
        if _chunk_type_name(chunk) != 'CCollisionMesh':
            continue
        for prop in getattr(chunk, 'PROPS', []) or []:
            if prop.theName != 'shapes':
                continue
            handles = getattr(prop, 'elements', None) or getattr(prop, 'Handles', None) or []
            for idx, handle in enumerate(handles):
                _assert_chunk_type(cr2w, handle.Reference, shape_types, f'CCollisionMesh.shapes[{idx}]')

def Build_SMeshSoundInfo_Chunk(cr2w, sound_info, parentID):
    CHUNK_INDEX = cr2w.HEADER.numChunks
    cr2w.HEADER.numChunks += 1
    cr2w.CR2WExport.append(CR2WExport(
        crc32=0,
        dataOffset=0,
        dataSize=0,
        name='SMeshSoundInfo',
        objectFlags=0,
        parentID=parentID,
        template=0))

    _chunk = W_CLASS(
        CR2WFILE=cr2w,
        idx=CHUNK_INDEX,
        PROPS=[],
        Type='SMeshSoundInfo',
        name='SMeshSoundInfo')

    if sound_info.get('soundTypeIdentification', ''):
        _chunk.PROPS.append(PROPERTY(
            theName='soundTypeIdentification',
            theType='CName',
            String=CSTRING(isUTF=False, String=sound_info['soundTypeIdentification'])))
    if sound_info.get('soundSizeIdentification', ''):
        _chunk.PROPS.append(PROPERTY(
            theName='soundSizeIdentification',
            theType='CName',
            String=CSTRING(isUTF=False, String=sound_info['soundSizeIdentification'])))
    if sound_info.get('soundBoneMappingInfo', ''):
        _chunk.PROPS.append(PROPERTY(
            theName='soundBoneMappingInfo',
            theType='CName',
            String=CSTRING(isUTF=False, String=sound_info['soundBoneMappingInfo'])))

    cr2w.CHUNKS.CHUNKS.append(_chunk)
    return CHUNK_INDEX


def BuildMesh(ALL_LODS, bone_data, common_info, col_mesh, strip_material_names=False):
    # col_mesh = [
    #     {
    #         'type': 'CCollisionShapeConvex',
    #         'physicalMaterialName': 'DefaultMaterial',
    #         'vertices': [[0.972583652, 2.10231471, 0.120589413, 1.0], [-0.0993768349, 4.15459871, 0.120589867, 1.0]],
    #         'polygons': [0, 1]
    #     },
    #     {
    #         'type': 'CCollisionShapeTriMesh',
    #         'physicalMaterialNames': ['Material.001', 'Material.002'],
    #         'vertices': [[1.33110976, 1.25678253, -1.526085, 1.0], [1.67450237, 1.48502111, 1.31386435, 1.0]],
    #         'triangles': [0, 1, 0],
    #         'physicalMaterialIndexes': [0, 1]
    #     }
    # ]
    
    
    #([24, 36, 1],[24, 36, 1]) #[numVertices, numIndices, useForShadowmesh]
    #get a single one for now
    cr2w = CR2W()
    cr2w.CNAMES = []
    cr2w.HEADER = CR2W_header(CRC32=0,
                            bufferSize=0,
                            buildVersion=1150341,
                            fileSize=0,
                            flags=0,
                            magic=1462915651,
                            numChunks=0,
                            timestamp=0,
                            version=162) #CR2WFile.HEADER
    cr2w.HEADER.timestamp  = 0
    
    #TODO don't actually need imports wolven kill will gen them
    cr2w.CR2WImport = [
        #CR2WImport(path = r"engine\materials\graphs\pbr_std.w2mg", className = "CMaterialGraph", flags = 0),
        #CR2WImport(path = r"engine\textures\editor\grey.xbm", className = "CBitmapTexture",flags = 0)
    ]

    cr2w.CR2W_Property = [CR2WProperty()]
    
    br = bStream(b'')
    first_vertex = 0
    first_index = 0
    for lod_idx, mesh_data in enumerate(ALL_LODS):
        mesh_settings = mesh_data[1]
        mesh_data = mesh_data[0]
        for idx, (bl_mesh_info, witcher_mat_info) in enumerate(mesh_data):
            #TODO checkLOD and useforshadowmesh
            numBonesPerVertex = 4
            vertex_skinning_entries_dict = defaultdict(list)
            
            if bone_data.jointNames: # check if any bones
                for entry in bl_mesh_info.skinningVerts:
                    if entry.boneId in bone_data.jointNames:# check if bone exists
                        entry.boneId_idx = bone_data.jointNames.index(entry.boneId)
                        vertex_skinning_entries_dict[entry.vertexId].append(entry)

            for i in range(bl_mesh_info.meshInfo.numVertices):
                br.writeFloat(bl_mesh_info.vertex3DCoords[i][0])    # x:float = 0.0
                br.writeFloat(bl_mesh_info.vertex3DCoords[i][1])    # y:float = 0.0
                br.writeFloat(bl_mesh_info.vertex3DCoords[i][2])   # z:float = 0.0
                first_vertex += 1
                entries = vertex_skinning_entries_dict[i]
                if len(entries) > numBonesPerVertex:
                    #entries = entries[:numBonesPerVertex]
                    log.critical('Found excess skinning data')
                remove_lowest_strength(entries)
                for entry in entries:
                    br.writeUInt8(entry.boneId_idx)
                for _ in range(numBonesPerVertex - len(entries)):
                    br.writeUInt8(0)    # bone_x:np.ubyte = 0
                for entry in entries:
                    br.writeFloat(entry.strength)
                for _ in range(numBonesPerVertex - len(entries)):
                    br.writeFloat(0.0)    # bone_x:np.ubyte = 0
                br.writeFloat(bl_mesh_info.normals[i][0])    # normx:float = 0.0
                br.writeFloat(bl_mesh_info.normals[i][1])    # normy:float = 0.0
                br.writeFloat(bl_mesh_info.normals[i][2])    # normz:float = 0.0
                
                do_lin2srgb = True

                # Keep uncooked rawVertices stride fixed. Redkit expects the same
                # byte layout regardless of useExtraStreams; that flag controls
                # cooking behavior/semantics, not whether these bytes exist.
                if do_lin2srgb:
                    br.writeUInt8(max(0, min(255, int(lin2srgb(bl_mesh_info.vertexColor[i][0]) * 255))))    # r:np.ubyte = 0.0
                    br.writeUInt8(max(0, min(255, int(lin2srgb(bl_mesh_info.vertexColor[i][1]) * 255))))    # g:np.ubyte = 0.0
                    br.writeUInt8(max(0, min(255, int(lin2srgb(bl_mesh_info.vertexColor[i][2]) * 255))))    # b:np.ubyte = 0.0
                else:
                    br.writeUInt8(max(0, min(255, int(bl_mesh_info.vertexColor[i][0] * 255))))    # r:np.ubyte = 0.0
                    br.writeUInt8(max(0, min(255, int(bl_mesh_info.vertexColor[i][1] * 255))))    # g:np.ubyte = 0.0
                    br.writeUInt8(max(0, min(255, int(bl_mesh_info.vertexColor[i][2] * 255))))    # b:np.ubyte = 0.0
                br.writeUInt8(max(0, min(255, int(bl_mesh_info.vertexColor[i][3] * 255))))    # a:np.ubyte = 0.0
                br.writeFloat(bl_mesh_info.UV_vertex3DCoords[i][0])    # ux:float = 0.0
                br.writeFloat(flip_v(bl_mesh_info.UV_vertex3DCoords[i][1]))    # uv:float = 0.0
                br.writeFloat(bl_mesh_info.UV2_vertex3DCoords[i][0])    # ux2:float = 0.0
                br.writeFloat(flip_v(bl_mesh_info.UV2_vertex3DCoords[i][1]))    # uv2:float = 0.0
                # br.write(bytes(88))
                br.writeFloat(bl_mesh_info.tangent_vector[i][0]) if True else br.writeFloat(0.0) #tangent
                br.writeFloat(bl_mesh_info.tangent_vector[i][1]) if True else br.writeFloat(0.0) #tangent
                br.writeFloat(bl_mesh_info.tangent_vector[i][2]) if True else br.writeFloat(0.0) #tangent
                
                br.writeFloat(bl_mesh_info.extra_vectors[i][0]) if True else br.writeFloat(0.0) #binormal
                br.writeFloat(bl_mesh_info.extra_vectors[i][1]) if True else br.writeFloat(0.0) #binormal
                br.writeFloat(bl_mesh_info.extra_vectors[i][2]) if True else br.writeFloat(0.0) #binormal
                
                br.writeFloat(bl_mesh_info.extra_vectors[i][3])  if False else br.writeFloat(0.0)
                br.writeFloat(bl_mesh_info.extra_vectors[i][4])  if False else br.writeFloat(0.0)
                br.writeFloat(bl_mesh_info.extra_vectors[i][5])  if False else br.writeFloat(0.0)
                br.writeFloat(bl_mesh_info.extra_vectors[i][6])  if False else br.writeFloat(0.0)
                br.writeFloat(bl_mesh_info.extra_vectors[i][7])  if False else br.writeFloat(0.0)
                br.writeFloat(bl_mesh_info.extra_vectors[i][8])  if False else br.writeFloat(0.0)
                br.writeFloat(bl_mesh_info.extra_vectors[i][9])  if False else br.writeFloat(0.0)
                br.writeFloat(bl_mesh_info.extra_vectors[i][10])  if False else br.writeFloat(0.0)
                br.writeFloat(bl_mesh_info.extra_vectors[i][11])  if False else br.writeFloat(0.0)
                br.writeFloat(bl_mesh_info.extra_vectors[i][12])  if False else br.writeFloat(0.0)
                br.writeFloat(bl_mesh_info.extra_vectors[i][13])  if False else br.writeFloat(0.0)
                br.writeFloat(bl_mesh_info.extra_vectors[i][14])  if False else br.writeFloat(0.0)
                br.writeFloat(bl_mesh_info.extra_vectors[i][15])  if False else br.writeFloat(0.0)
                br.writeFloat(bl_mesh_info.extra_vectors[i][16])  if False else br.writeFloat(0.0)
                br.writeFloat(bl_mesh_info.extra_vectors[i][17])  if False else br.writeFloat(0.0)
                br.writeFloat(bl_mesh_info.extra_vectors[i][18])  if False else br.writeFloat(0.0)
                
                #br.write(bytes(76))
                # br.writeFloat(bl_mesh_info.bitangent_vector[i][0])    # some_x:float = 0.0
                # br.writeFloat(bl_mesh_info.bitangent_vector[i][1])    # some_y:float = 0.0
                # br.writeFloat(bl_mesh_info.bitangent_vector[i][2])    # some_z:float = 0.0
                # br.write(bytes(64))
        
    br.seek(0, os.SEEK_SET)
    vert_buffer = br.fhandle.read()
    br.close()

    br = bStream(b'')
    for mesh_data in ALL_LODS:
        mesh_settings = mesh_data[1]
        mesh_data = mesh_data[0]
        for (bl_mesh_info, witcher_mat_info) in mesh_data:
            for face in bl_mesh_info.faces:
                br.writeUInt16(face[2])
                br.writeUInt16(face[1])
                br.writeUInt16(face[0])
                first_index +=3
    br.seek(0, os.SEEK_SET)
    i_buffer = br.fhandle.read()

    cr2w.CR2WBuffer = [CR2WBuffer(index = 1, diskSize = len(vert_buffer), memSize = len(vert_buffer)), CR2WBuffer(index = 2, diskSize = len(i_buffer), memSize = len(i_buffer))]
    cr2w.BufferData = [vert_buffer, i_buffer] # CR2WFile.BufferData
    cr2w.CHUNKS = DATA()

    cr2w.CR2WExport = []

    MATERIAL_DICT = {}
    for mesh_data in ALL_LODS:
        mesh_settings = mesh_data[1]
        mesh_data = mesh_data[0]
        for (mesh_chunk, material) in mesh_data:
            if material:
                mat = material[0]
            else:
                #fallback material
                mat = {
                    'name' : 'Material0',
                    'witcher_props' : {
                        'name':'Material',
                        'enableMask': False,
                        'local':False,
                        #'base':'custom',
                        'base_custom':'engine\\materials\\graphs\\pbr_std.w2mg',
                        'input_props':[],
                    }
                }
            if mat['name'] not in MATERIAL_DICT.keys():
                MATERIAL_DICT[mat['name']] = mat
            else:
                log.debug("MATERIAL_DICT entry skipped (no name)")

    # Strip material names to Material0, Material1, etc.
    if strip_material_names:
        import re
        name_map = {}
        new_dict = {}
        for idx, (old_name, mat_data) in enumerate(MATERIAL_DICT.items()):
            # Try to find "MaterialN" inside the existing name first
            # e.g. "meshName_Material2" -> "Material2", "Material0.001" -> "Material0"
            match = re.search(r'(Material\d+)', old_name)
            new_name = match.group(1) if match else f"Material{idx}"
            # Avoid duplicate names from different materials resolving to the same MaterialN
            if new_name in new_dict:
                new_name = f"Material{idx}"
            name_map[old_name] = new_name
            mat_data['name'] = new_name
            new_dict[new_name] = mat_data
        MATERIAL_DICT = new_dict
        # Update material names in ALL_LODS mesh data so chunk materialID lookup matches
        for mesh_data in ALL_LODS:
            for (mesh_chunk, material) in mesh_data[0]:
                if material and material[0]['name'] in name_map:
                    material[0]['name'] = name_map[material[0]['name']]

    #?CHUNKS
    Build_CMesh_Chunk(cr2w, ALL_LODS, bone_data = bone_data, common_info = common_info, MATERIAL_DICT = MATERIAL_DICT)

    
    # build local instance chunks
    for inst in MATERIAL_DICT.values():
        if inst['witcher_props']['local']:
            for prop in inst['witcher_props']['input_props']:
                if prop['name'] == 'Roughness' and prop['type'] == 'TEX_IMAGE':
                    inst['witcher_props']['input_props'].remove(prop)
                elif prop['name'] == 'Alpha' and prop['type'] == 'TEX_IMAGE':
                    inst['witcher_props']['input_props'].remove(prop)
            Build_CMaterialInstance_Chunk(cr2w, inst)
    

    # Build collision chunks if col_mesh is provided
    if col_mesh:
        # CCollisionMesh comes after CMesh + local material instance chunks.
        collision_mesh_idx = cr2w.HEADER.numChunks
        shape_chunk_indices = list(
            range(collision_mesh_idx + 1, collision_mesh_idx + 1 + len(col_mesh))
        )
        collision_mesh_idx = Build_CCollisionMesh_Chunk(cr2w, shape_chunk_indices, parentID=1)  # parentID=1 for CMesh #0
        _assert_chunk_type(cr2w, collision_mesh_idx, {'CCollisionMesh'}, 'CMesh.collisionMesh')

        cmesh_chunk = cr2w.CHUNKS.CHUNKS[0]
        _upsert_cmesh_handle_prop(
            cmesh_chunk,
            prop_name='collisionMesh',
            prop_type='handle:CCollisionMesh',
            chunk_index=collision_mesh_idx
        )
        _register_child_link(cr2w, 0, collision_mesh_idx)

        # parentID is 1-based index of the parent chunk
        collision_parent_id = collision_mesh_idx + 1
        for shape_data in col_mesh:
            if isinstance(shape_data, CCollisionShapeConvex):
                Build_CCollisionShapeConvex_Chunk(cr2w, shape_data, parentID=collision_parent_id)
            elif isinstance(shape_data, CCollisionShapeTriMesh):
                Build_CCollisionShapeTriMesh_Chunk(cr2w, shape_data, parentID=collision_parent_id)
            elif isinstance(shape_data, CCollisionShapeBox):
                Build_CCollisionShapeBox_Chunk(cr2w, shape_data, parentID=collision_parent_id)
            elif isinstance(shape_data, CCollisionShapeSphere):
                Build_CCollisionShapeSphere_Chunk(cr2w, shape_data, parentID=collision_parent_id)
            elif isinstance(shape_data, CCollisionShapeCapsule):
                Build_CCollisionShapeCapsule_Chunk(cr2w, shape_data, parentID=collision_parent_id)
            else:
                raise TypeError(f"Unsupported collision shape type: {type(shape_data).__name__}")

    # Build SMeshSoundInfo chunk if sound info is provided
    sound_info = common_info.get('soundInfo', None)
    if sound_info and sound_info.get('enabled', False):
        sound_info_idx = Build_SMeshSoundInfo_Chunk(cr2w, sound_info, parentID=1)  # parentID=1 for CMesh #0
        _assert_chunk_type(cr2w, sound_info_idx, {'SMeshSoundInfo'}, 'CMesh.soundInfo')

        cmesh_chunk = cr2w.CHUNKS.CHUNKS[0]
        _upsert_cmesh_handle_prop(
            cmesh_chunk,
            prop_name='soundInfo',
            prop_type='ptr:SMeshSoundInfo',
            chunk_index=sound_info_idx
        )
        _register_child_link(cr2w, 0, sound_info_idx)

    _validate_mesh_chunk_links(cr2w)

    return cr2w
