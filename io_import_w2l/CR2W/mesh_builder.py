
import os
import re

from .bStream import bStream
from .dc_mesh import lin2srgb, srgb2lin
from .setup_logging import *
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
                            STRING,
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


def flip_v(vf):
    return (vf*-1)+1

def printProps(PROPS):
    for prop in PROPS:
        try:
            if hasattr(prop, 'isUTF'):
                print(f"STRING (String = '{prop.String}', isUTF='{prop.isUTF}')")
            elif hasattr(prop, 'Value'):
                print(f"PROPERTY(Value = {prop.Value}, theName='{prop.theName}', theType='{prop.theType}')")

            elif hasattr(prop, 'ValueA'):
                print(f"PROPERTY(ValueA = {prop.ValueA}, theName='{prop.theName}', theType='{prop.theType}')")
        
            elif hasattr(prop, 'String'):
                print(f"PROPERTY(theName='{prop.theName}', theType='{prop.theType}', String = STRING(isUTF = {prop.String.isUTF}, String = '{prop.String.String}'))")
            elif hasattr(prop, 'DateTime'):
                print(f"PROPERTY(theName='{prop.theName}', theType='{prop.theType}', DateTime = CDATETIME(Value = {prop.DateTime.Value}, String = '{prop.DateTime.String}'))")
            elif hasattr(prop, 'elements'):
                print(f"#!ARRAY START")
                print(f"array = PROPERTY( theName='{prop.theName}', theType='{prop.theType}', elements = [])")
                print('array.elements.append()')
                printProps(prop.elements)
                print(f"#?ARRAY END")
            elif hasattr(prop, 'MoreProps'):
                print(f"#!!MoreProps START")
                try:
                    print(f"more_prop_{prop.theName} = PROPERTY( theName='{prop.theName}', theType='{prop.theType}', MoreProps=[])")
                except Exception as e:
                    try:
                        print(f"more_prop_{prop.__class__.__name__} = PROPERTY( theName='{prop.__class__.__name__}', theType='{prop.__class__.__name__}', MoreProps=[])")
                    except Exception as e:
                        raise e
                printProps(prop.MoreProps) if hasattr(prop, 'MoreProps') else None
                print(f"#? MoreProps END")
            elif hasattr(prop, 'More'):
                print(f"#!!More START")
                try:
                    print(f"more_prop_{prop.theName} = PROPERTY( theName='{prop.theName}', theType='{prop.theType}', More=[])")
                except Exception as e:
                    try:
                        print(f"more_prop_{prop.__class__.__name__} = PROPERTY( theName='{prop.__class__.__name__}', theType='{prop.__class__.__name__}', More=[])")
                    except Exception as e:
                        raise e
                printProps(prop.More) if hasattr(prop, 'More') else None
                print(f"#? More END")
            elif hasattr(prop, 'PROPS'):
                print(f"#!!PROPS START")
                try:
                    print(f"more_prop_{prop.theName} = PROPERTY( theName='{prop.theName}', theType='{prop.theType}', PROPS=[])")
                except Exception as e:
                    try:
                        print(f"more_prop_{prop.__class__.__name__} = PROPERTY( theName='{prop.__class__.__name__}', theType='{prop.__class__.__name__}'), PROPS=[])")
                    except Exception as e:
                        raise e
                printProps(prop.PROPS) if hasattr(prop, 'PROPS') else None
                print(f"#? PROPS END")
            else:
                print(f"#!UNK PROP VAL, theName='{prop.theName}', theType='{prop.theType}'")

        except Exception as e:
            print(f"#!FAILED", e)

    return None
    

def print_CR2W(PROPS):
    #CR2WFile.CHUNKS.CHUNKS[0].PROPS
    print(f"~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
    print(f"################ START OF PROP LIST ####################")
    print(f"########################################################")

    printProps(PROPS)
    
    print(f"########################################################")
    print(f"################ END OF PROP LIST ######################")
    print(f"~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")


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
        _CMesh_chunk.PROPS.append(PROPERTY(theName='importFile', theType='String', String = STRING(isUTF = False, String = 'D:\Dev\box.hkx')))

    def add_importFileTimeStamp():
        _CMesh_chunk.PROPS.append(PROPERTY(theName='importFileTimeStamp', theType='CDateTime', DateTime = CDATETIME(Value = 247518305951179776, String = '2010/01/26 13:47:23')))  

    def add_materialNames(material_dict):
        array = PROPERTY( theName='materialNames', theType='array:2,0,String', elements = [])
        for mat_data in material_dict.values():
            array.elements.append(STRING(String = mat_data['name'], isUTF='False'))
        _CMesh_chunk.PROPS.append(array)

    def add_authorName():
        _CMesh_chunk.PROPS.append(PROPERTY(theName='authorName', theType='String', String = STRING(isUTF = False, String = 'No Author Name')))
            
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
        _CMesh_chunk.PROPS.append(PROPERTY(Value = common_info['lod0_MeshSettings'].isTwoSided, theName='isTwoSided', theType='Bool'))

    def add_collisionMesh():
        pass

    #This seems to determine if UV2 and vertex color is added to the cook
    def add_useExtraStreams(common_info):
        _CMesh_chunk.PROPS.append(PROPERTY(Value = common_info['lod0_MeshSettings'].useExtraStreams, theName='useExtraStreams', theType='Bool'))

    def add_generalizedMeshRadius(common_info):
        _CMesh_chunk.PROPS.append(PROPERTY(Value = common_info['generalizedMeshRadius'], theName='generalizedMeshRadius', theType='Float'))

    def add_mergeInGlobalShadowMesh(common_info):
        _CMesh_chunk.PROPS.append(PROPERTY(Value = common_info['lod0_MeshSettings'].mergeInGlobalShadowMesh, theName='mergeInGlobalShadowMesh', theType='Bool'))

    def add_isOccluder():
        _CMesh_chunk.PROPS.append(PROPERTY(Value = False, theName='isOccluder', theType='Bool'))

    def add_smallestHoleOverride():
        pass

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

            if useForShadowmesh:
                more_prop_SMeshChunkPacked.MoreProps.append(PROPERTY(Value = useForShadowmesh, theName='useForShadowmesh', theType='Bool'))

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

    def add_isStatic(theBool):
        _CMesh_chunk.PROPS.append(PROPERTY(Value = theBool, theName='isStatic', theType='Bool'))

    def add_entityProxy(common_info):
        _CMesh_chunk.PROPS.append(PROPERTY(Value = common_info['lod0_MeshSettings'].entityProxy, theName='entityProxy', theType='Bool'))

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
    add_collisionMesh() if True else None
    add_useExtraStreams(common_info) if True else None
    add_generalizedMeshRadius(common_info) if True else None
    add_mergeInGlobalShadowMesh(common_info) if True else None
    add_isOccluder() if True else None
    add_smallestHoleOverride() if True else None
    
    
    chunks_to_make = []
    for lod_idx, mesh_data in enumerate(ALL_LODS):
        mesh_settings = mesh_data[1]
        mesh_data = mesh_data[0]
        for idx, (bl_mesh_info, witcher_mat_info) in enumerate(mesh_data):
            vertex_type = EMeshVertexType.EMVT_SKINNED if bone_data.jointNames else EMeshVertexType.EMVT_STATIC #EMeshVertexType
            mat_name = witcher_mat_info[0]['name'] if witcher_mat_info else 'Material0'
            materialID = list(MATERIAL_DICT.keys()).index(mat_name)
            numBonesPerVertex = 4 #Uint8 0
            numVertices = bl_mesh_info.meshInfo.numVertices #Uint32
            numIndices = bl_mesh_info.meshInfo.numIndices #Uint32
            firstVertex = bl_mesh_info.meshInfo.firstVertex # Uint32 0
            firstIndex = bl_mesh_info.meshInfo.firstIndex # Uint32 0
            renderMask = 0 # EMeshChunkRenderMask 
            useForShadowmesh = False #Bool False
            
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
    add_isStatic(True) if not bone_data.jointNames else False
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

def BuildMesh(ALL_LODS, bone_data, common_info):
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
            bl_mesh_info.meshInfo.firstVertex = first_vertex
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
                
                if do_lin2srgb:
                    br.writeUInt8(int(lin2srgb(bl_mesh_info.vertexColor[i][0]) * 255))    # r:np.ubyte = 0.0
                    br.writeUInt8(int(lin2srgb(bl_mesh_info.vertexColor[i][1]) * 255))    # g:np.ubyte = 0.0
                    br.writeUInt8(int(lin2srgb(bl_mesh_info.vertexColor[i][2]) * 255))    # b:np.ubyte = 0.0
                else:
                    br.writeUInt8(int(bl_mesh_info.vertexColor[i][0] * 255))    # r:np.ubyte = 0.0
                    br.writeUInt8(int(bl_mesh_info.vertexColor[i][1] * 255))    # g:np.ubyte = 0.0
                    br.writeUInt8(int(bl_mesh_info.vertexColor[i][2] * 255))    # b:np.ubyte = 0.0
                    
                br.writeUInt8(int(bl_mesh_info.vertexColor[i][3] * 255))    # a:np.ubyte = 0.0
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
            bl_mesh_info.meshInfo.firstIndex = first_index
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
                print('MATERIAL_DICT')

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
    
    return cr2w