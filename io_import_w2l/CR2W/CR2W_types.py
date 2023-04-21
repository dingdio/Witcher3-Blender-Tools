import csv
import functools
import os
import base64
import time
import numpy as np
from pathlib import Path
from enum import Enum
from typing import List

from .Types.CMesh import CMesh
from .Types.VariousTypes import CPaddedBuffer, CNAME, CNAME_INDEX, CMatrix4x4, NAME, CBufferVLQInt32, CColor
from .json_convert.CR2WJsonObject import CR2WJsonData, CR2WJsonChunkMap, CR2WJsonMap, CR2WJsonScalar, CR2WJsonArray
from .W3Strings import LoadStringsManager
from .bStream import *
from .setup_logging import *
from .witcher_cache import bundle
log = logging.getLogger(__name__)

from .CR2W_helpers import Enums
from .TypeList import get_vectors
from .bin_helpers import (
                        ReadBit6,
                        ReadFloat16,
                        ReadFloat24,
                        ReadVLQInt32,
                        detectedFloat,
                        getStringOfLen,
                        readFloat,
                        readInt16,
                        readInt32,
                        readSByte,
                        readString,
                        read_wstring,
                        getString,
                        readU32,
                        readU64,
                        readUByte,
                        readUByteCheck,
                        readUChar,
                        readUShort,
                        readU32Check,
                        FileSize,
                        readUShortCheck,
                        skipPadding)

Entity_Type_List = ["CEntity",
                    "CGameplayEntity",
                    "W3LockableEntity",
                    "W3Container",
                    "W3AnimatedContainer",
                    "W3LockableEntity",
                    "W3NewDoor",
                    "CItemEntity",
                    "CWitcherSword",
                    #actor entities
                    "CR4Player",
                    "CPlayer",
                    "CActor",
                    "CNewNPC",
                    "W3PlayerWitcher",
                    "W3ReplacerCiri",
                    "CCamera",
                    
                    #####!WITCHER_2
                    "CDoor",
                    "CContainer",
                    "CActionPoint",
                    #"CDeniedAreaComponent",
                    ]

v_types = [
    'String',
    'Float',
    'Bool',
    'Uint32',
    'Uint16',
    'Uint8'
]
IREDPrimitive = [
    'CString',
    'CFloat',
    'CBool',
    'CUInt32',
    'CUInt16',
    'CUInt8'
]


def detectedProp(f, CR2WFILE, offset ):
    gName = ""
    gType = ""
    if (f.tell()+4 >= FileSize(f)):
        return 0;
    gNameIdx = readUShortCheck(f, f.tell())
    gTypeIdx = readUShortCheck(f, f.tell()+2);

    try:
        if (hasattr(CR2WFILE.CNAMES[gNameIdx], 'name')):
            gName = CR2WFILE.CNAMES[gNameIdx].name.value;
            CR2WFILE.gName = gName
            if (hasattr(CR2WFILE.CNAMES[gTypeIdx], 'name')):
                gType = CR2WFILE.CNAMES[gTypeIdx].name.value;
                CR2WFILE.gType = gType
    except IndexError:
        pass
        #log.debug('Not valid index')
    #return 1
    #CR2WFILE.CNAMES[THEINDEX].name.value

    return ((gName != gType and gName != "" and gType != "") and gType !="resourceVersion"
    and "Ref:" not in gName and gName != "PLATFORM_PC" and gType != "cookingPlatform"
    and gName != "ECookingPlatform" and "Uint" not in gName
    and not (gNameIdx > 255 and gTypeIdx > 255 and readUByteCheck(f,f.tell()) == 0 and readUShortCheck(f, f.tell()+3) <= CR2WFILE.CR2WTable[1].itemCount));
# }


def getClass(f, CR2WFILE, self, idx):
    self.currentClass = ""#TODO FIX THIS #CR2WFILE.CNAMES[i].name.value;
    f.seek(CR2WFILE.CR2WExport[idx].dataOffset + CR2WFILE.start)
    zero = readSByte(f)
    if zero != 0:
        if (zero == 1):
            cake = readInt32(f)
        elif (zero == -128):
            dzero2 = ReadBit6(f)
            log.warning("WARANING: -128")
        else:
            log.warning("WARANING: Found not zero for class")
    return W_CLASS(f, CR2WFILE, self, idx)

class DATA:
    def __init__(self, f = None, CR2WFILE = None, anim_name = None, **kwargs):
        self.exports=[]
        self.sizes=[]
        self.CHUNKS=[]
        self.animCount = 0

        if f:
            self.Read(f, CR2WFILE, anim_name)
        else:
            self.Create(kwargs)
    def Create(self, args):
        pass

    def Write(self):
        raise NotImplementedError('')

    def Read(self, f, CR2WFILE, anim_name):
        for i in range(0, CR2WFILE.CR2WTable[4].itemCount):
            # exports[idx] = CR2WFILE.CR2WExport[idx].dataOffset + 1 + start;
            # sizes[idx] = CR2WFILE.CR2WExport[idx].dataSize;
            self.exports.append(CR2WFILE.CR2WExport[i].dataOffset + 1 + CR2WFILE.start)
            self.sizes.append(CR2WFILE.CR2WExport[i].dataSize)

        for i in range(0, CR2WFILE.CR2WTable[4].itemCount):
            self.currentClass = ""#TODO FIX THIS #CR2WFILE.CNAMES[i].name.value;
            f.seek(CR2WFILE.CR2WExport[i].dataOffset + CR2WFILE.start);
            zero = readSByte(f)
            if zero != 0:
                if (zero == 1):
                    cake = readInt32(f)
                elif (zero == -128):
                    dzero2 = ReadBit6(f)
                    log.warning("WARANING: -128")
                else:
                    log.warning("WARANING: Found not zero for class")
            #self.Class = W_CLASS(f, CR2WFILE, self)
            start_time = time.time()
            self.CHUNKS.append(W_CLASS(f, CR2WFILE, self, i))
            time_taken = time.time() - start_time
            log.debug('%i Read Chunk %s in %f seconds.',i, self.CHUNKS[-1].name, time.time() - start_time)

            if anim_name: #"w2anims" in CR2WFILE.fileName:
                animations = self.CHUNKS[0].GetVariableByName('animations').value
                for set_entry_idx in animations:
                    chunk_entry = getClass(f, CR2WFILE, self, set_entry_idx-1)
                    anim_idx = chunk_entry.GetVariableByName('animation').Value
                    chunk_anim = getClass(f, CR2WFILE, self, anim_idx-1)
                    entry_name = chunk_anim.GetVariableByName('name').ToString()
                    if entry_name == anim_name:
                        self.CHUNKS.append(chunk_entry)
                        self.CHUNKS.append(chunk_anim)
                        buffer_idx = chunk_anim.GetVariableByName('animBuffer').Value
                        chunk_buffer = getClass(f, CR2WFILE, self, buffer_idx-1)
                        self.CHUNKS.append(chunk_buffer)
                        chunk_entry.ChunkIndex = 2
                        chunk_entry.GetVariableByName('animation').Value = 3
                        chunk_anim.ChunkIndex = 3
                        chunk_anim.GetVariableByName('animBuffer').Value = 4
                        chunk_buffer.ChunkIndex = 4
                        self.CHUNKS[0].PROPS[0].value = [2]
                        break
                break
                # if idx < 1 or idx == 101:
                #     pass
                # else:
                #     f.seek(self.classEnd)
                #     return
            if time_taken > 0.3:
                log.warn("Time taken more than 0.3")
            #log.debug(' Read Chunk in %f seconds.', time.time() - start_time)


class STRINGINDEX:
    def __init__(self, f, CR2WFILE, parent):
        """The string index.

        Keyword arguments:
        f -- file to read
        CR2WFILE -- the CR2WFILE
        """
        self.Index = readUShort(f)# ushort Index;
        if self.Index == 0:
            self.String = None
            return None
        # local int lvl <hidden=true> = level;
        try:
            if (self.Index > 0 and CR2WFILE.CNAMES[self.Index].name.value):
                self.String = CR2WFILE.CNAMES[self.Index].name.value
        except IndexError:
            pass #couldn't find name index

        try:
            if (self.Index > 0 and not hasattr(parent, 'dataType') and hasattr(CR2WFILE, 'CR2WImport') and CR2WFILE.CR2WImport[self.Index-1].path):
                self.Path = CR2WFILE.CR2WImport[self.Index-1].path
        except IndexError:
            pass #couldn't fix path index
    def ToString(self):
        if hasattr(self, 'String'):
            return self.String
        elif hasattr(self, 'Path'):
            return self.Path

class FLOATVALUE:
    def __init__(self, f, CR2WFILE, parent):
        self.Type = PROPSTART(f, CR2WFILE, parent)
        self.Value = readFloat(f) # float

class Data_Bytes:
    def __init__(self, f, size):
        f.seek(f.tell() + size - 4)

class PROPSTART_BLANK:
    def __init__(self):
        self.size = 0
        self.dataType = 0
        self.name = ""
        self.type = ""

class PROPSTART_NO_NAME:
    def __init__(self, f, CR2WFILE, parent):
        self.size = readU32(f)
        self.dataType = STRINGINDEX(f, CR2WFILE, self)
        self.name = CR2WFILE.CNAMES[self.dataType.Index].name.value
        self.type = CR2WFILE.CNAMES[self.dataType.Index].name.value

class PROPSTART:
    def __init__(self, f, CR2WFILE, parent):
        try:
            self.strIdx = STRINGINDEX(f, CR2WFILE, self) #<name="String Index">;
            if self.strIdx.Index > 0:
                self.dataType = STRINGINDEX(f, CR2WFILE, self) #<name="Data Type">;
                self.name = CR2WFILE.CNAMES[self.strIdx.Index].name.value; #string
                self.type = CR2WFILE.CNAMES[self.dataType.Index].name.value; #string

                if CR2WFILE.HEADER.version <= 115:
                    unk = readInt16(f)
                    if unk != -1:
                        self.type = CR2WFILE.CNAMES[unk].name.value; #string #!
                        #f.seek(-2, os.SEEK_CUR)
                        #raise Exception("W2 PROPSTART")

                if ("rRef:" in self.type and (readU32Check(f.tell()-8) == 10)):
                    self.size = 2; #local int
                elif (f.tell() + 4 < FileSize(f) and readU32Check(f, f.tell()) < FileSize(f) - f.tell() + 2):
                    self.size = readU32(f) #uint32
                else:
                    self.size = 4; # local ushort

                if (self.size > 4):
                    startofData_Bytes = f.tell()
                    self.Data = Data_Bytes(f, self.size)
                    f.seek(startofData_Bytes)
            else:
                self.dataType = None
                self.name = None
                self.type = None
        except Exception as e:
            raise e


def sizeof(sizeof): #TODO FIX
    return sizeof

def CDate2String(value):
    day, month, year, ms, s, m, h = 0, 0, 0, 0, 0, 0, 0;
    dt = "";
    value >>= 0xA;
    day = (value & 0x1F) + 1; value >>= 0x5;
    month = (value & 0x1F) + 1; value >>= 0x5;
    year = (value & 0xFFF); value >>= 0xC;
    #Time
    ms = value & 0x3FF; value >>= 0xA;
    s = value & 0x3F; value >>= 0x6;
    m = value & 0x3F; value >>= 0x6;
    h = value & 0X1FF;
    #SPrintf( dt, "%04d/%02d/%02d %02d:%02d:%02d", year, month, day, h, m, s );
    dt = "%04d/%02d/%02d %02d:%02d:%02d" % (year, month, day, h, m, s)
    return dt;

class CDATETIME:
    def __init__(self, f = None, **kwargs):
        if f and not kwargs:
            self.Value = readU64(f) #uint64;
            self.String = CDate2String(self.Value) #local string String = CDate2String(Value);
        else:
            self.Value:np.uint64 = kwargs['Value']
            self.String = kwargs['String']

class STRINGANSI:
    def __init__(self, f):
        len = readUChar(f)
        self.isUTF = False
        if (len >= 128):
            len = len - 128
            self.isUTF = True
            len = len*2

        if (self.isUTF):
            self.String = f.read(len).decode('utf-16')

        else:
            self.String = f.read(len).decode('utf-8')
            self.String = self.String.replace("\x00", "") #remove \x00
    def ToString(self):
        return self.String


class LocalizedString(object):
    """docstring for LocalizedString."""
    def __init__(self):
        self.val = 0
        self.text = ''
    def Read(self, f, CR2WFILE):
        self.val = str(readU32(f)) #! temp
        self._get_text()
        return self
    def _get_text(self):
        string_manager = LoadStringsManager()
        self.text = string_manager.GetString(int(self.val))
        return self.text

class STRING:
    def __init__(self, f = None, **kwargs):
        if f and not kwargs:
            self.Read(f)
        else:
            self.Create(kwargs)

    def Create(self, args):
        self.isUTF = False
        self.String = ''
        for arg in args.items():
            setattr(self, arg[0], arg[1])

    def Read(self, f):
        startofString = f.tell()
        strLen = readUChar(f) #uchar
        len = strLen
        actualLen = 0 #local uint
        flag = 0 #local uint
        maxSize = 0 #local uint
        self.isUTF = False
        if(strLen >= 128): #128??
            len = len - 128
            if (len >= 64):
                len = len - 64
                len = readUChar(f)*64 + len
            self.String = f.read(len).decode('utf-8')
        else:
            self.isUTF = True
            if (len >= 64):
                len = len - 64
                len = readUChar(f)*64 + len
            len = len*2
            self.String = f.read(len).decode('utf-16')
            log.warning("Invalid length for string at %u\n", f.tell());
    def ToString(self):
        return self.String

def doesExist(str, str2):
    return str2 in str

def exists(obj, path):
    try:
        functools.reduce(getattr, path.split("."), obj)
        return True
    except AttributeError:
        return False
def endsWith(str, str2):
    return str.endswith(str2)

class ELEMENT:
    def GetVariableByName(self, str):
        for item in self.MoreProps:
            if item.theName == str:
                return item
        return None
    def __init__(self, f, CR2WFILE, parent):
        self.elementName  = "" #local string
        firstProp  = "" #local string
        prevProp  = "" #local string
        Sub_Element_Count  = 0 #local uint
        self.ElementIdx  = parent.ElementCounter; #local uint
        skipNextTime  = False #local int
        sfp  = -1 #local int
        title  = "" #local string
        pos = 0 #local int
        self.classEnd = parent.classEnd
        if exists(parent, "Count"):
            self.Count = parent.Count
        self.MoreProps = []
        while(f.tell() < parent.dataEnd and f.tell() < parent.classEnd):
            if (detectedProp(f, CR2WFILE,f.tell())):
                # if (doMesh == True)
                #     parseMesh();

                #parse elements:
                if (prevProp != "metalLevelsOut" and detectedProp(f, CR2WFILE, f.tell()) and CR2WFILE.gName != firstProp and CR2WFILE.gName) : #multilayer_layers should end at metalLevelsOut
                    Sub_Element_Count += 1
                    if (firstProp == ""):
                        firstProp = CR2WFILE.gName
                    More =PROPERTY(f,CR2WFILE, self)#struct PROPERTY More  #//sub property
                    self.MoreProps.append(More)
                    if (self.elementName == "" and exists(More, "Index") and (exists(More, "Type.type") and More.Type.type == "CName")):
                        try:
                            self.elementName = More.Index.String#More.Index[0].String
                        except IndexError:
                            pass
                    if (CR2WFILE.gName == "material" and title == "" and exists(More , "Path.Path")):
                        title = More.Path.Path
                    elif (exists(More, "Type") and title == "" and not doesExist(parent.Type.type, "layer")):
                        if (More.Type.name == "id" or endsWith(More.Type.name, "Id") or endsWith(More.Type.type, "Id")):
                            if (exists(More, "Value")):
                                #SPrintf(title, "%s = %Ld", More.Type.name, More.Value)
                                title = "%s = %Ld" % (More.Type.name, More.Value)
                            elif (exists(More, "More[0].Value")):
                                #SPrintf(title, "%s = %Ld", More.Type.name, More.More[0].Value)
                                title = "%s = %Ld" % (More.Type.name, More.More[0].Value)
                        elif (doesExist(More.Type.type, "Ref") and exists(More.Path)):
                            title = More.Path.Path
                    if (not doesExist(CR2WFILE.gType, "loat") and not doesExist(CR2WFILE.gType, "Uint8")):
                        pass #SetBackColor(cNone)

                    if (parent.theType == "meshLocalMaterialHeader" and CR2WFILE.gName == "size"):
                        parent.ElementCounter+=1
                        break
                else:
                    if (parent.lastProp == ""):
                        parent.lastProp = prevProp

                    prevProp = CR2WFILE.gName
                    parent.ElementCounter+=1
                    break
                prevProp = CR2WFILE.gName
            else:
                f.seek(1,1)#FSkip(1);

class Cr2wResourceManager:
    resourceManager = None
    def __init__(self):

        fileDir = os.path.dirname(os.path.realpath(__file__))
        fileDir = os.path.join(fileDir, "witcher_cache")
        filename = os.path.join(fileDir, "pathhashes.csv")
        if not os.path.exists(filename):
            bundle.create_pathhashes(outputPath=filename)
            log.info('Creating pathhashes.csv')
        self.pathashespath = filename
        #self.HashdumpDict = {}
        reader = csv.DictReader(open(self.pathashespath))

        self.HashdumpDict = {}
        for row in reader:
            self.HashdumpDict[row["HashInt"]] = row["Path"]
    @staticmethod
    def Get():
        if (Cr2wResourceManager.resourceManager == None):
            Cr2wResourceManager.resourceManager = Cr2wResourceManager()
        return Cr2wResourceManager.resourceManager

class CSectorDataResource:
    def __init__(self, f, CR2WFILE, parent):
        self.box0 = readFloat(f)
        self.box1 = readFloat(f)
        self.box2 = readFloat(f)
        self.box3 = readFloat(f)
        self.box4 = readFloat(f)
        self.box5 = readFloat(f)
        self.hashint = readU64(f)
        if self.hashint == 0:
            self.pathHash = 0
        else:
            resoruce = Cr2wResourceManager.Get()
            if str(self.hashint) in resoruce.HashdumpDict:
                self.pathHash = resoruce.HashdumpDict[str(self.hashint)]
            else:
                log.critical("FOUND UNKNOWN PATH")
                self.pathHash = self.hashint
        #public CString pathHash;
class CSectorDataObject:
    def __init__(self, f, CR2WFILE, parent):
        self.type = readUByte(f) #CUInt8
        self.flags = readUByte(f) #CUInt8
        self.radius = readUShort(f) #CUInt16
        self.offset = readU64(f) #CUInt64
        self.position = CVector3D(f, 0)
        # self.positionX = readFloat(f) #CFloat
        # self.positionY = readFloat(f) #CFloat
        # self.positionZ = readFloat(f) #CFloat
        if self.type == 0:
            log.debug("found invalid")

class SBlockDataCollisionObject:
    def __init__(self, f, size= None, packedObjectType= None):
        self.meshIndex = readUShort(f) #CUInt16
        self.padding = readUShort(f) #CUInt16
        self.collisionMask = readU64(f) #CUInt64
        self.collisionGroup = readU64(f) #CUInt64

class SBlockDataDimmer(object):
    """docstring for SBlockDataDimmer."""
    def __init__(self, f, size= None, packedObjectType= None):
        self.ambienLevel = readFloat(f)
        self.marginFactor = readFloat(f)
        self.dimmerType = readUByte(f)
        self.paddin1 = readUByte(f)
        self.paddin2 = readUShort(f)

class SBlockDataMeshObject:
    def __init__(self, f, size = None, packedObjectType= None):
        #base.Read(file, size);
        self.meshIndex = readUShort(f) # CUInt16
        self.forceAutoHide = readUShort(f) # CUInt16
        self.lightChanels = readUByte(f) # CUInt8
        self.forcedLodLevel = readUByte(f) # CUInt8
        self.shadowBias = readUByte(f) # CUInt8
        self.renderingPlane = readUByte(f) # CUInt8



class SBlockDataRigidBody:
    def __init__(self, f, size= None, packedObjectType= None):
        #base.Read(file, size);
        self.meshIndex = readUShort(f) # CUInt16
        self.forceAutoHide = readUShort(f) # CUInt16
        self.lightChanels = readUByte(f) # CUInt8
        self.forcedLodLevel = readUByte(f) # CUInt8
        self.shadowBias = readUByte(f) # CUInt8
        self.renderingPlane = readUByte(f) # CUInt8

        self.linearDamping = readFloat(f) #CFloat
        self.angularDamping = readFloat(f) #CFloat
        self.linearVelocityClamp = readFloat(f) #CFloat
        self.collisionMask = readU64(f) #CUInt64
        self.collisionGroup = readU64(f) #CUInt64
        self.motionType = readUByte(f) #CUInt8
        self.padd1 = readUByte(f) #CUInt8
        self.padd2 = readUByte(f) #CUInt8
        self.padd3 = readUByte(f) #CUInt8

class SBlockDataPointLight(object):
    """docstring for SBlockDataPointLight."""
    def __init__(self):
        self.color = None #CUInt32
        self.radius = None #CFloat
        self.brightness = None #CFloat
        self.attenuation = None #CFloat
        self.autoHideRange = None #CFloat
        self.shadowFadeDistance = None #CFloat
        self.shadowFadeRange = None #CFloat
        self.shadowFadeBlendFactor = None #CFloat
        self.lightFlickering = None #SVector3D
        self.shadowCastingMode = None #CUInt8
        self.dynamicShadowsFaceMask = None #CUInt8
        self.envColorGroup = None #CUInt8
        self.padding = None #CUInt8
        self.lightUsageMask = None #CUInt32

    def Read(self, f):
        self.color = CColor().Read(f) #readU32(f) #CUInt32
        self.radius = readFloat(f) #CFloat
        self.brightness = readFloat(f) #CFloat
        self.attenuation = readFloat(f) #CFloat
        self.autoHideRange = readFloat(f) #CFloat
        self.shadowFadeDistance = readFloat(f) #CFloat
        self.shadowFadeRange = readFloat(f) #CFloat
        self.shadowFadeBlendFactor = readFloat(f) #CFloat
        self.lightFlickering = CVector3D(f, 0) #SVector3D
        self.shadowCastingMode = readUByte(f) #CUInt8
        self.dynamicShadowsFaceMask = readUByte(f) #CUInt8
        self.envColorGroup = readUByte(f) #CUInt8
        self.padding = readUByte(f) #CUInt8
        self.lightUsageMask = readU32(f) #CUInt32
        return self


class SBlockDataSpotLight: #make CPointLightComponent
    def __init__(self):
        self.color = None #CUInt32
        self.radius = None #CFloat
        self.brightness = None #CFloat
        self.attenuation = None #CFloat
        self.autoHideRange = None #CFloat
        self.shadowFadeDistance = None #CFloat
        self.shadowFadeRange = None #CFloat
        self.shadowFadeBlendFactor = None #CFloat
        self.lightFlickering = None # SVector3D
        self.shadowCastingMode = None # CUInt8
        self.dynamicShadowsFaceMask = None # CUInt8
        self.envColorGroup = None # CUInt8
        self.padding = None # CUInt8
        self.lightUsageMask = None # CUInt32
        self.innerAngle = None # CFloat
        self.outerAngle = None # CFloat
        self.softness = None # CFloat
        self.projectionTextureAngle = None # CFloat
        self.projectionTexureUBias = None # CFloat
        self.projectionTexureVBias = None # CFloat
        self.projectionTexture = None # CUInt16
        self.padding2 = None # CUInt16

    def Read(self, f):
        self.color = CColor().Read(f) #CUInt32
        self.radius = readFloat(f) #CFloat
        self.brightness = readFloat(f) #CFloat
        self.attenuation = readFloat(f) #CFloat
        self.autoHideRange = readFloat(f) #CFloat
        self.shadowFadeDistance = readFloat(f) #CFloat
        self.shadowFadeRange = readFloat(f) #CFloat
        self.shadowFadeBlendFactor = readFloat(f) #CFloat
        self.lightFlickering = CVector3D(f,0) # SVector3D
        self.shadowCastingMode = readUByte(f) # CUInt8
        self.dynamicShadowsFaceMask = readUByte(f) # CUInt8
        self.envColorGroup = readUByte(f) # CUInt8
        self.padding = readUByte(f) # CUInt8
        self.lightUsageMask = readU32(f) # CUInt32
        self.innerAngle = readFloat(f) # CFloat
        self.outerAngle = readFloat(f) # CFloat
        self.softness = readFloat(f) # CFloat
        self.projectionTextureAngle = readFloat(f) # CFloat
        self.projectionTexureUBias = readFloat(f) # CFloat
        self.projectionTexureVBias = readFloat(f) # CFloat
        self.projectionTexture = readUShort(f) # CUInt16
        self.padding2 = readUShort(f) # CUInt16
        return self

class SBlockDataParticles():
    def __init__(self, f):
        self.particleSystem = readUShort(f) # CUInt16
        self.padding = readUShort(f) # CUInt16
        self.lightChanels = readUByte(f) # CUInt8
        self.renderingPlane = readUByte(f) # CUInt8
        self.envAutoHideGroup = readUByte(f) # CUInt8
        self.transparencySortGroup = readUByte(f) # CUInt8
        self.globalEmissionScale = readFloat(f) # CFloat

class SBlockDataDecal():
    def __init__(self, f):
        self.diffuseTexture = readUShort(f) # CUInt16
        self.padding = readUShort(f) # CUInt16
        self.specularColor = readU32(f) # CUInt32
        self.normalTreshold = readFloat(f) # CFloat
        self.specularity = readFloat(f) # CFloat
        self.fadeTime = readFloat(f) # CFloat

class SBlockData:
    def __init__(self, f, size, packedObjectType):
        startp = f.tell()
        self.rotationMatrix = CMatrix3x3(f)
        self.position = CVector3D(f, 0) #CVector3D
        self.streamingRadius = readUShort(f) #CUInt16
        self.flags = readUShort(f) #CUInt16
        self.occlusionSystemID = readU32(f) #CUInt32
        self.packedObjectType = packedObjectType


        if packedObjectType == Enums.BlockDataObjectType.Mesh:
            self.packedObject = SBlockDataMeshObject(f)
            self.resourceIndex = self.packedObject.meshIndex
        elif packedObjectType == Enums.BlockDataObjectType.Collision: # actuall rigid bodies?
            self.packedObject = SBlockDataCollisionObject(f)
            self.resourceIndex = self.packedObject.meshIndex
        elif packedObjectType == Enums.BlockDataObjectType.Decal:
            self.packedObject = SBlockDataDecal(f)
        elif packedObjectType == Enums.BlockDataObjectType.Dimmer:
            self.packedObject = SBlockDataDimmer(f)
        elif packedObjectType == Enums.BlockDataObjectType.PointLight:
            self.packedObject = SBlockDataPointLight().Read(f)
        elif packedObjectType == Enums.BlockDataObjectType.SpotLight:
            self.packedObject = SBlockDataSpotLight().Read(f)
        elif packedObjectType == Enums.BlockDataObjectType.RigidBody: # actuall rigid bodies?
            self.packedObject = SBlockDataRigidBody(f)
            self.resourceIndex = self.packedObject.meshIndex
        elif packedObjectType == Enums.BlockDataObjectType.Cloth:
            f.seek(size - 56, 1)
        elif packedObjectType == Enums.BlockDataObjectType.Destruction:
            f.seek(size - 56, 1)
        elif packedObjectType == Enums.BlockDataObjectType.Particles:
            self.packedObject = SBlockDataParticles(f)
        elif packedObjectType == Enums.BlockDataObjectType.Invalid:
            f.seek(size - 56, 1)
        else:
            f.seek(size - 56, 1)
            #self.tail = #CBytes
            #f.seek(size - 56, 1)


        endp = f.tell()
        read = endp - startp
        if (read < size):
            pass
        elif(read > size):
            print("read too far")
        # endp = f.tell()
        # read = endp - startp
        # if (read < size):
        #     f.seek(size - read, 1)#unk1.Read(file, size - (uint)read);
        # elif (read > size):
        #     log.error("ERROR READING SBlockDataMeshObject")

class CMatrix3x3:
    def __init__(self, f):
        self.ax = readFloat(f)
        self.ay = readFloat(f)
        self.az = readFloat(f)
        self.bx = readFloat(f)
        self.by = readFloat(f)
        self.bz = readFloat(f)
        self.cx = readFloat(f)
        self.cy = readFloat(f)
        self.cz = readFloat(f)

class CVector3D:
    def __init__(self, f, compression):
        self.x = 0.0
        self.y = 0.0
        self.z = 0.0
        if (compression == 0):
            self.x = readFloat(f)
            self.y = readFloat(f)
            self.z = readFloat(f)
        if (compression == 1):
            self.x = ReadFloat24(f)
            self.y = ReadFloat24(f)
            self.z = ReadFloat24(f)
        if (compression == 2):
            self.x = ReadFloat16(f)
            self.y = ReadFloat16(f)
            self.z = ReadFloat16(f)
    def getList(self):
        return [self.x, self.y, self.z]

class HANDLE:
    def ToString(self):
        if self.DepotPath:
            return self.DepotPath
        else:
            return self

    def GetRef(self, CHUNKS):
        return CHUNKS[self.Reference]

    def __init__(self, f = None, CR2WFILE = None, parent = None, **kwargs):
        self.ChunkHandle = False
        self.Reference = None
        self.val = None # Int32
        self.DepotPath = None
        self.ClassName = None
        self.Flags = None
        self.theType = None
        self.Index = None
        if f and not kwargs:
            self.Read(f, CR2WFILE, parent)
        else:
            self.__CR2WFILE = CR2WFILE
            self.Create(kwargs)

    def Create(self, args):
        for arg in args.items():
            setattr(self, arg[0], arg[1])
        if self.ChunkHandle == True:
            currentchun = len(self.__CR2WFILE.CHUNKS.CHUNKS)
            if not hasattr(self.__CR2WFILE, 'childrendict'):
                self.__CR2WFILE.childrendict = {}
            if currentchun not in self.__CR2WFILE.childrendict:
                self.__CR2WFILE.childrendict[currentchun] = []
            self.__CR2WFILE.childrendict[currentchun].append(self.Reference)

    def Read(self, f, CR2WFILE, parent):
        self.ChunkHandle = False
        self.Reference = None
        self.val = readInt32(f) # Int32
        self.DepotPath = None
        self.ClassName = None
        self.Flags = None
        if hasattr(parent, 'theType'):
            self.theType = 'handle:'+parent.theType.split(':')[-1]
        else:
            self.theType = 'handle:'+parent.Type
        self.Index = None
        val = self.val

        if (val >= 0):
            self.ChunkHandle = True
        if (self.ChunkHandle):
            if (val == 0):
                self.Reference = None
            else:
                self.Reference = self.val - 1 #CR2WFILE
                #Reference = cr2w.chunks[val - 1];
        else:
            try:
                self.DepotPath = CR2WFILE.CR2WImport[-val - 1].path

                filetype = CR2WFILE.CR2WImport[-val - 1].className
                self.ClassName = CR2WFILE.CNAMES[filetype].name.value

                self.Flags = CR2WFILE.CR2WImport[-val - 1].flags
            except:
                f.seek(-4,1)
                self.Index = readU32(f)#uint ;
                log.warning("WARNING: HANDLE depo index error")

class CByteArray:
    def __init__(self, f):
        self.arraysize = readU32(f)
        self.Bytes = f.read(self.arraysize)


class CByteArray2(object):
    """docstring for CByteArray2."""
    def __init__(self):
        super(CByteArray2, self).__init__()
        self.arraysize = None
        self.Bytes = None
    def Read(self, f, size):
        self.arraysize = readU32(f)
        self.Bytes = f.read(self.arraysize - 4)


class SFoliageInstanceData:
    def __init__(self, dumb = False):
        self.X = 0
        self.Y = 0
        self.Z = 0
        self.Yaw = 0
        self.Pitch = 0
        self.Roll = 0

    def Read(self, f, size = 0):
        self.X = readFloat(f)
        self.Y = readFloat(f)
        self.Z = readFloat(f)
        self.Yaw = readFloat(f)
        self.Pitch = readFloat(f)
        self.Roll = readFloat(f)
        return self

class EngineTransform:
    def __init__(self, f = None, **kwargs):
        self.X = 0.0
        self.Y = 0.0
        self.Z = 0.0
        self.Pitch = 0.0
        self.Yaw = 0.0
        self.Roll = 0.0
        self.Scale_x = 1.0
        self.Scale_y = 1.0
        self.Scale_z = 1.0
        if f:
            self.Read(f)

    def Read(self, f):
        flags = readSByte(f)

        if ((flags & 1) == 1):
            self.X = readFloat(f)
            self.Y = readFloat(f)
            self.Z = readFloat(f)

        if ((flags & 2) == 2):
            self.Pitch = readFloat(f)
            self.Yaw = readFloat(f)
            self.Roll = readFloat(f)

        if ((flags & 4) == 4):
            self.Scale_x = readFloat(f)
            self.Scale_y = readFloat(f)
            self.Scale_z = readFloat(f)
    @classmethod
    def from_json(cls, **kwargs):
        t = cls()
        for name, val in kwargs.items():
            setattr(t, name, val)
        return t

class CEngineQsTransform:
    def __init__(self, f):
        flags = readUByte(f)

        self.x = 0.0
        self.y = 0.0
        self.z = 0.0
        self.pitch = 0.0
        self.yaw = 0.0
        self.roll = 0.0
        self.w = 1.0
        self.scale_x = 0.0
        self.scale_y = 0.0
        self.scale_z = 0.0

        if ((flags & 1) == 1):
            self.x = readFloat(f)
            self.y = readFloat(f)
            self.z = readFloat(f)

        if ((flags & 2) == 2):
            self.pitch = readFloat(f)
            self.yaw = readFloat(f)
            self.roll = readFloat(f)
            self.w = readFloat(f)

        if ((flags & 4) == 4):
            self.scale_x = readFloat(f)
            self.scale_y = readFloat(f)
            self.scale_z = readFloat(f)

# class SAnimationBufferBitwiseCompressedData(object):
#     """docstring for SAnimationBufferBitwiseCompressedData."""
#     def __init__(self, arg):
#         super(SAnimationBufferBitwiseCompressedData, self).__init__()
#     arg

class SAnimationBufferBitwiseCompressedBoneTrack(object):
    """docstring for SAnimationBufferBitwiseCompressedBoneTrack."""
    def __init__(self, CR2WFILE):
        super(SAnimationBufferBitwiseCompressedBoneTrack, self).__init__()
        self.CR2WFILE = CR2WFILE
    def Read(self, f, size, classEnd = 9999):
        self.classEnd = classEnd
        f.seek(1,1)#zero= readUByte(f)
        self.position = PROPERTY(f,self.CR2WFILE, self)
        self.orientation = PROPERTY(f,self.CR2WFILE, self)
        self.scale = PROPERTY(f,self.CR2WFILE, self)
        f.seek(2,1)#zero2= readUByte(f)
        #zero3= readUByte(f)
        return self

class CEnum(object):
    def __init__(self, CR2WFILE, IsFlag = False):
        self.IsFlag = IsFlag
        self.CR2WFILE = CR2WFILE
        self.String = None
        self.strings = []
    def Read(self, f):
        self.strings = []
        if self.IsFlag:
            while True:
                idx = readUShort(f)
                if (idx == 0):
                    break
                s = self.CR2WFILE.CNAMES[idx].name.value
                self.strings.append(s)
        else:
            idx = readUShort(f)
            s = self.CR2WFILE.CNAMES[idx].name.value
            self.String = s
            self.strings.append(s)
    def ToString(self):
        return ''.join(self.strings)


class dotdict(dict):
    """dot.notation access to dictionary attributes"""
    __getattr__ = dict.get
    __setattr__ = dict.__setitem__
    __delattr__ = dict.__delitem__

class PROPERTY:
    def ToString(self):
        if ("CName" in self.theType):
            return self.Index.ToString()
        elif ("StringAnsi" in self.theType or "String" in self.theType):
            return self.String.ToString()
        elif ("handle:" in self.theType):
            return self.Handles[0].ToString()
        elif hasattr(self, 'Index'):
            try:
                return self.Index.ToString()
            except Exception as e:
                raise e
        else:
            log.warning("Returned None PROP string")
            return None

    def ToArray(self):
        if ("2,0,handle" in self.theType):
            return self.Handles

    def GetVariableByName(self, str):
        for item in self.More:
            if item.theName == str:
                return item
        return None

    def __init__(self, f = None, CR2WFILE = None, parent = None, no_name = False, custom_propstart=False, **kwargs):
        self.Type = None #<CR2W.CR2W_types.PROPSTART>
        self.theName = None #'importFile'
        self.theType = None #'String'

        if f and not kwargs:
            self.Read(f, CR2WFILE, parent, no_name, custom_propstart)
        else:
            self.__CR2WFILE = CR2WFILE
            self.Create(kwargs)

    def Create(self, args):
        for arg in args.items():
            setattr(self, arg[0], arg[1])

    def Write(self, f):
        raise NotImplementedError('No Prop Writing')

    def Read(self, f = None, CR2WFILE = None, parent = None, no_name = False, custom_propstart=False):
        if custom_propstart:
            Type = custom_propstart
        else:
            if no_name:
                Type = PROPSTART_NO_NAME(f,CR2WFILE, self)
            else:
                Type = PROPSTART(f,CR2WFILE, self)
        if Type.type == None:
            self.Type = None
            return None
        strartofthis = f.tell()

        dataEnd = f.tell() + Type.size - 4 #local uint64
        count = 1 #local uint64
        theType = Type.type
        self.theType = Type.type
        self.theName = Type.name #local string
        arrayDataType = ""
        arrayType = "" #local string
        str = "" #local string
        path = "" #local string
        propCount = 0 #local uint

        self.Type = Type
        self.dataEnd = dataEnd
        self.classEnd = parent.classEnd

        if theType == "array:2,0,CGUID":
            log.debug("array:2,0,CGUID")

        More = []
        #? to make the animation list load faster save reading the bones until needed
        #if ("array:129,0,SAnimationBufferBitwiseCompressedBoneTrack" == Type.type):
        if (",SAnimationBufferBitwiseCompressedBoneTrack" in Type.type):
            self.Count = readU32(f)
            for _ in range(0, self.Count):
                More.append(SAnimationBufferBitwiseCompressedBoneTrack(CR2WFILE).Read(f, 0, self.classEnd))
            self.More = More
            return
        elif (Type.type in Enums.Enum_Flags_Types):
            the_enum = CEnum(CR2WFILE, True)
            the_enum.Read(f)
            self.IsFlag = the_enum.IsFlag
            self.strings = the_enum.strings
            return
        elif (Type.type in Enums.Enum_Types):
            the_enum = CEnum(CR2WFILE, False)
            the_enum.Read(f)
            self.Index = the_enum
            return
        elif (",SMeshChunkPacked" in Type.type):
            self.chunks = CArray(CR2WFILE, SMeshChunkPacked)
            self.chunks.Read(f, 0)
            return
        elif ("SAnimationBufferBitwiseCompressedData" == Type.type):
            #zero1 = readSByte(f)
            f.seek(1,1)
            this_end = dataEnd - 2
            while f.tell() != this_end:
                name = CR2WFILE.CNAMES[readUShort(f)].name.value; #string
                type = CR2WFILE.CNAMES[readUShort(f)].name.value; #string
                size = readU32(f)
                if type == "Uint16":
                    More.append(dotdict({'theName':name,'Value':readUShort(f)}))
                elif type == "Float":
                    More.append(dotdict({'theName':name,'Value':readFloat(f)}))
                elif type == "Uint32":
                    More.append(dotdict({'theName':name,'Value':readU32(f)}))
                elif type == "Bool" or type == "Int8":
                    More.append(dotdict({'theName':name,'Value':readUByte(f)}))
                else:
                    log.critical("Unknown value in bone data")
                #More.append(PROPERTY(f,CR2WFILE, self)) #struct PROPERTY More;
            self.More = More
            #zero2 = readUByte(f)
            #zero3 = readUByte(f)
            f.seek(2,1)
            #f.seek(dataEnd)
            return
        elif (theType == "array:2,0,CGUID"):
            self.Count = readU32(f)
            for _ in range(0, self.Count):
                More.append(CGUID().Read(f))
            self.More = More
            f.seek(dataEnd)
        elif (theType == "CGUID"):
            self.GUID = CGUID().Read(f)
            f.seek(dataEnd)
        elif ("array" in theType or "static:" in theType or "curveData" in theType or "]" in theType):
            if (":" in theType):
                delim = theType.find(':')
                arrayDataType = theType[0:delim] #SubStr( theType, 0, delim);
            else:
                delim = theType.find(']')
                arrayDataType = theType[delim+1:len(theType)]#SubStr( theType, delim+1, len(theType) - delim);
            arrayType = theType[delim+1:len(theType)]#SubStr( theType, delim+1, len(theType) - delim);
            theType = arrayType;
            if (f.tell()+2 < FileSize(f) and theType != "inkWidgetLibraryItem" and readU32Check(f, f.tell()) != 0 and (readU32Check(f, f.tell())) + f.tell() < dataEnd):
                if (readUShortCheck(f, f.tell()) == 0):
                    f.seek(2,1);
                    self.Count = readUShort(f)
                else:
                    self.Count = readU32(f)
                count = self.Count;

        #parse data:
        if ("curveData" in arrayDataType):
            pass
        elif ("handle" in Type.type): #//sub-class sorting
            if CR2WFILE.HEADER.version <= 115:
                if (count == 1 and "array" not in Type.type):
                    self.Handles = []
                    for _ in range(0,count):
                        self.Handles.append(HANDLE(f,CR2WFILE,self))
                        if self.Handles[0].ChunkHandle:
                            self.Value = self.Handles[0].val
                else:
                    elementTypeName = STRINGINDEX(f,CR2WFILE, self)
                    unk2 = readInt16(f)
                    if unk2 != -1:
                        raise ValueError('Unexpected value for unk2')
                    #tell = f.tell()
                    self.Handles = []
                    for _ in range(0,count):
                        self.Handles.append(HANDLE(f,CR2WFILE,self))
                f.seek(-1,os.SEEK_CUR)
            else:
                if (doesExist(Type.type, "]")):
                    f.seek(8,1)
                    count = (int)((strartofthis + Type.size + 4 - f.tell()) / 4)
                self.Handles = []
                startofHandles = f.tell()
                for _ in range(0, count):
                    self.Handles.append(HANDLE(f,CR2WFILE, self))
                f.seek(startofHandles); # FSeek(startof(Handles));
        elif (Type.type == "array:String" or Type.type == "array:2,0,String"):
                if CR2WFILE.HEADER.version <= 115:
                    elementTypeName = STRINGINDEX(f,CR2WFILE, self)
                    unk2 = readInt16(f)
                    if unk2 != -1:
                        raise ValueError('Unexpected value for unk2')
                    self.elements = []
                    for _ in range(0,count):
                        self.elements.append(STRING(f))
                else:
                    self.elements = []
                    for _ in range(0,count):
                        self.elements.append(STRING(f)) #<name="submesh">; #not needed remove later
        elif ("array:array" in Type.type):
            pass
        elif ("StringAnsi" in Type.type):
            self.String = STRINGANSI(f)
        elif ("String" in Type.type or theType == "NodeRef"):
            if (theType == "LocalizedString"):
                self.String = LocalizedString().Read(f,CR2WFILE)
                CR2WFILE.LocalizedStrings.append(self.String)
                #self.Hash=readU64(f); #uint64
            else:
                self.String = STRING(f)
        elif ("Ref:" in Type.type ):
            pass
        elif (theType == "TweakDBID" ):
            pass
        if (theType == "CName" ):
            if (readUShortCheck(f, f.tell()) == 0):
                f.seek(2,1);
            if (count == 1):
                # //if (detectedFloat(FTell()))
                # //    float Value; //not sure why but some floats are called CNames (main_colors.inkstyle -> Briefings)
                # //else
                self.Index = STRINGINDEX(f,CR2WFILE, self);
            else:
                self.Index = []
                if (Type.name == "chunkMaterials"):
                    for _ in range(0,count):
                        self.Index.append(STRINGINDEX(f,CR2WFILE,self)) #<name="submesh">; #not needed remove later
                else:
                    for _ in range(0,count):
                        self.Index.append(STRINGINDEX(f,CR2WFILE,self))

        # elif (count > 0 and count == boneCount and doesExist(Lower(Type.name), "bone")):
        #     pass
        elif (theType == "Matrix" ):
            pass
        elif (theType == "EngineTransform" ):
            self.EngineTransform = EngineTransform(f)
        #TODO move functions around so this isn't needed and sub array without direct Type info can be detected using parent
        elif ("EngineQsTransform" in theType and "array" in theType):
            self.value=[]
            for _ in range(0,count):
                subCount = readU32(f)
                subArray = []
                for _ in range(0, subCount):
                    subArray.append(CEngineQsTransform(f))
                self.value.append(subArray)
        elif ("EngineQsTransform" in theType):
            if (count == 1 and "array" not in Type.type):
                self.Value = CEngineQsTransform(f)
            else:
                self.value=[]
                for _ in range(0,count):
                    self.value.append(CEngineQsTransform(f))
        elif ("Int8" == theType):
            if (count == 1 and "array" not in Type.type):
                self.Value = readSByte(f)
            else:
                self.value=[]
                for _ in range(0,count):
                    self.value.append(readSByte(f))
        elif ("nt8" in theType and "array" not in Type.type):
            if (count == 1):
                self.Value = readUByte(f)
            else:
                self.value=[]
                for _ in range(0,count):
                    self.value.append(readUByte(f))
        elif ("Uint16" in theType):
            if (count == 1 and "array" not in Type.type):
                self.Value = readUShort(f)
            else:
                self.value=[]
                for _ in range(0,count):
                    self.value.append(readUShort(f))
        elif ("Int16" in theType):
            if (count == 1 and "array" not in Type.type):
                self.Value = readInt16(f) #readUShort(f)
            else:
                self.value=[]
                for _ in range(0,count):
                    self.value.append(readInt16(f))#(readUShort(f))
        elif ("Uint32" in theType):
            if (count == 1 and "array" not in Type.type):
                self.Value = readU32(f)
            else:
                self.value=[]
                for _ in range(0,count):
                    self.value.append(readU32(f))
            return
        elif ("Int32" in theType):
            if (count == 1 and "array" not in Type.type):
                self.Value = readInt32(f)
            else:
                self.value=[]
                for _ in range(0,count):
                    self.value.append(readInt32(f))
        elif (theType == "CDateTime"):
            self.DateTime = CDATETIME(f)
        elif (theType == "Float"):
            if (count == 3 and (exists(parent, "Type") and doesExist(parent.Type.name,"olor"))
            or (Type.type == "[3]Float" and Type.name == "v" and exists(parent, "ElementIdx"))) :
                #SetForeColor(byteR);
                # float Red; SetForeColor((uint64)(byteG)<<8);
                # float Green; SetForeColor((uint64)(byteB)<<16);
                # float Blue; SetForeColor(cNone);
                self.Red = readFloat(f)
                self.Green = readFloat(f)
                self.Blue = readFloat(f)
            elif (count >= 2) :
                #for (z=0; z<count; z++):
                for _ in range(0, count):
                    startofValue = f.tell()
                    self.Value = readFloat(f) # float Value;
                    if (not detectedFloat(f, startofValue)):
                        f.seek(-4,1)#FSkip(-4);
                        self.ValueAsInt = readInt32(f)#int ValueAsInt;
            elif (count == 1):
                if (detectedFloat(f, f.tell())):
                    self.Value = readFloat(f)#float Value;
                else:
                    self.Value = readInt32(f)#int32 Value;
            else:
                self.value = []
                for _ in range(0, count):
                    self.value.append(readFloat(f))#float value[count];
        elif (theType == "Vector3"):
            pass
        elif (theType == "Vector2"):
            pass
        elif (theType == "Vector4"):
            pass
        elif ("Flags" in Type.type):
            pass
        elif (theType == "Uint64"):
            if (count == 1 and "array" not in Type.type):
                self.Value = readU64(f) # uint64
            else:
                self.value = []
                for _ in range(0,count):
                    self.value.append(readU64(f)) # uint64
        elif (theType == "Bool"):
            if (count == 1 and "array" not in Type.type):
                self.Value = readUByte(f) # uint64
            else:
                self.value = []
                for _ in range(0,count):
                    self.value.append(readUByte(f)) # uint64
            return
        elif("ptr:" in Type.type ):
                if (count == 1 and "array" not in Type.type):
                    if CR2WFILE.HEADER.version <= 115:
                        self.Handles = []
                        for _ in range(0,count):
                            self.Handles.append(HANDLE(f,CR2WFILE,self))
                    else:
                        self.Value = readU32(f)
                else:
                    if CR2WFILE.HEADER.version <= 115:
                        elementTypeName = STRINGINDEX(f,CR2WFILE, self)
                        unk2 = readInt16(f)
                        if unk2 != -1:
                            raise ValueError('Unexpected value for unk2')
                        tell = f.tell()
                        self.Handles = []
                        for _ in range(0,count):
                            self.Handles.append(HANDLE(f,CR2WFILE,self))
                    else:
                        self.value = []
                        for _ in range(0,count):
                            self.value.append(readU32(f))
        elif theType == "SharedDataBuffer":
            self.Bufferdata = CByteArray(f)
            #self.PackageHdr = PROPSTART(f, CR2WFILE, self); f.seek(4,1) #FSkip(4); #//start of new CR2W
            #return
        elif ("ataBuffer" in Type.type ):
            if (Type.size == 8 or Type.size == 6):
                self.ValueA = readUShort(f) #ushort ValueA <name="Buffer Number">;
                # if (self.ValueA > 0 and self.ValueA <= CR2WFILE.CR2WTable[5].itemCount):
                #     buffers[self.ValueA-1] = i+1;
                #     buffers2[self.ValueA-1] = Type.strIdx.Index;
            else:
                #f.seek(Type.size -10, 1) #FSkip(Type.size -10);
                # tell = f.tell()
                f.seek(dataEnd)
        elif ("TagList" == theType):
            self.TagList = []
            count = ReadBit6(f)
            for _ in range(0, count):
                self.TagList.append(CR2WFILE.CNAMES[readUShort(f)].name) #LIST OF CNAMES
            propCount+=1
        elif (dataEnd - f.tell() == 4):# //unknown non-arrays
            if (detectedFloat(f, f.tell())):
                self.Value = readFloat(f) #float ;
            else:
                self.Value = readU32(f) #uint32 ;
        elif (dataEnd - f.tell() == 2):#
            self.Index = STRINGINDEX(f,CR2WFILE, self)
        elif (dataEnd - f.tell() == 1):#
            if (sizeof(Type.size) == 4):
                self.Value = readUByte(f) #ubyte
        elif (dataEnd - f.tell() == 8):#
            if (sizeof(Type.size) == 4):
                self.Value = readU64(f) #uint64
        elif (count):#  exists(count) it always declared??           //unknown arrays
            if ((dataEnd - f.tell()) / 4 == count):#
                self.value = []
                if (detectedFloat(f, f.tell())):
                    for _ in range(0, count):
                        self.value.append(readFloat(f)) #float value[count];
                else:
                    for _ in range(0, count):
                        self.value.append(readU32(f)) #uint32 value[count];
            elif ((dataEnd - f.tell()) / 2 == count):#
                self.Index = []
                for _ in range(0, count):
                    self.Index.append(STRINGINDEX(f,CR2WFILE, self)) #STRINGINDEX Index[count];
            elif ((dataEnd - f.tell()) == count):#
                self.value = []
                for _ in range(0, count):
                    self.value.append(readUByte(f)) #ubyte value[count];


        #//local string subPropIdentifier <hidden=false> = "";
        self.lastProp = "";#local string
        self.ElementCounter = 0;#local int
        self.theType = Type.type# theType
        if ("array:2,0,CEntityAppearance" in Type.type):
            for _ in range(0, count):
                More.append(ELEMENT(f,CR2WFILE, self))
            self.More = More
            propCount+=1
        if parent.classEnd == None:
            pass #print('WARNING: Attempting generic prop read without class size info')
        while (parent.classEnd is not None and f.tell() < parent.classEnd and f.tell() < dataEnd and f.tell() < FileSize(f)-4 and readU32Check(f, f.tell()) != 1462915651):
            if (detectedProp(f,CR2WFILE, f.tell()) and count > 1 and not hasattr(self, "Value") and not hasattr(self, "Index")): #!exists(this.Value) && !exists(this.Index)) :
                #setColor();
                #self.More = ELEMENT(f) #ELEMENT More;
                More.append(ELEMENT(f,CR2WFILE, self))
                self.More = More
                propCount+=1
            else:
                if ( detectedProp(f, CR2WFILE, f.tell()) ):
                    if (CR2WFILE.CNAMES[readUShortCheck(f, f.tell()+2)].name == "SharedDataBuffer" ):
                        PackageHdr = PROPSTART(f, CR2WFILE, self); f.seek(4,1);#FSkip(4); #//start of new CR2W
                        return
                    else:
                        if (f.tell() < dataEnd):
                            # if (doMesh == True and count == 1):
                            #     pass #parseMesh();
                            #setColor();
                            More.append(PROPERTY(f,CR2WFILE, self)) #struct PROPERTY More;  #//sub property
                            self.More = More
                            propCount+=1
                            # if (not doesExist(gType, "loat") && not doesExist(gType, "Uint8"))
                            #     SetBackColor(cNone);
                        else:
                            break
                else:
                    f.seek(1,1)
        #f.seek(dataEnd,1)

class CQuaternion:
    def __init__(self, f):
        self.x = readFloat(f)
        self.y = readFloat(f)
        self.z = readFloat(f)
        self.w = readFloat(f)
    def __iter__(self):
        return iter(['x','y','z','w'])
    def __getitem__(self, item):
        return getattr(self, item)

class SSkeletonRigData:
    def __init__(self, f):
        self.position = CQuaternion(f)
        self.rotation = CQuaternion(f)
        self.scale = CQuaternion(f)

class CCompressedBuffer:
    def __init__(self, f, CR2WFILE, parent, Name = "rigData"):
        self.Name = Name
        self.parent = parent
        self.CR2WFILE = CR2WFILE
        self.rigData = []
    def Read(self, f, size, count):
        m_count = count
        #tell = f.tell()
        f.seek(2,1)
        for _ in range(0, m_count):
            self.rigData.append(SSkeletonRigData(f))

import uuid

class CGUID(object):
    """docstring for CGUID."""
    def __init__(self):
        super(CGUID, self).__init__()
        self._guid = bytearray(16)
    @property
    def GuidString(self):
        return str(self)
    @GuidString.setter
    def GuidString(self, value):
        try:
            g = uuid.UUID(bytes_le=value)
            self._guid = g.bytes
        except ValueError:
            pass
    def Read(self, f: bStream):
        self._guid = f.read(16)
        return self
    def __str__(self):
        if self._guid is not None and len(self._guid) > 0:
            return str(uuid.UUID(bytes_le=self._guid))
        else:
            self._guid = uuid.uuid4().bytes
            return str(self)

class SEntityBufferType1():
    def __init__(self, CR2WFILE, BufferV1, idx):
        self.ComponentName = False#new CName(cr2w, this, nameof(ComponentName)) { IsSerialized = true };
        self.Guid = CGUID() #new CGUID(cr2w, this, nameof(Guid)) { IsSerialized = true };
        self.Buffer = CByteArray2() #new CByteArray2(cr2w, this, nameof(Buffer)) { IsSerialized = true };
    def CanRead(self, CR2WFILE, f):
        self.ComponentName = STRINGINDEX(f, CR2WFILE, self) #CR2WFILE.CNAMES[readUShort(f)].name
        if self.ComponentName.Index != 0:
            return True
        return False

    def Read(self, f, size = 0):
        if self.ComponentName.Index:
            self.Guid.Read(f)
            self.Buffer.Read(f, size)

class SEntityBufferType2():
    def __init__(self, CR2WFILE):
        self.CR2WFILE = CR2WFILE
        self.componentName = False #new CName(cr2w, this, nameof(componentName)) {IsSerialized = true};
        self.sizeofdata = False #new CUInt32(cr2w, this, nameof(sizeofdata)) { IsSerialized = true };
        self.variables = CBufferUInt32(CR2WFILE, CVariantSizeTypeName) #new CBufferUInt32<CVariantSizeTypeName>(cr2w, this, nameof(variables)) { IsSerialized = true };
    def Read(self, f, size):
        self.sizeofdata = readU32(f)
        self.componentName = self.CR2WFILE.CNAMES[readUShort(f)].name
        self.variables.Read(f, size)

class CVariantSizeTypeName():
    def __init__(self, CR2WFILE):
        self.CR2WFILE = CR2WFILE
        self.PROP = False
    def Read(self, f, size):
        varsize = readU32(f)#file.ReadUInt32();
        self.classEnd = varsize
        buffer = f.read(varsize - 4)#file.ReadBytes((int)varsize - 4);
        br = bStream(data = bytearray(buffer))
        typeId = readUShort(br)
        nameId = readUShort(br)

        if (nameId == 0):
            return

        typename = self.CR2WFILE.CNAMES[typeId].name.value
        varname = self.CR2WFILE.CNAMES[nameId].name.value
        propstart = PROPSTART_BLANK()
        propstart.size = varsize
        propstart.name = varname
        propstart.type = typename
        self.PROP = PROPERTY(br, self.CR2WFILE, self, False, propstart)

class CBufferUInt32():
    def __init__(self, CR2WFILE, buffer_type):
        self.buffer_type = buffer_type
        self.CR2WFILE = CR2WFILE
        self.elements = []
    def Read(self, f, size):
        elementcount = readU32(f)
        for _ in range(0, elementcount):
            element = self.buffer_type(self.CR2WFILE)
            element.Read(f, 0)
            self.elements.append(element)

class SFoliageResourceData():
    def __init__(self, CR2WFILE):
        self.CR2WFILE = CR2WFILE

    def Read(self, f, size):
        self.TreeType = HANDLE(f, self.CR2WFILE, self) # CSRTBaseTree Class
        self.TreeCollection = CBufferVLQInt32(self.CR2WFILE, SFoliageInstanceData)
        self.TreeCollection.Read(f, 0)

        #CBufferVLQInt32<SFoliageInstanceData> TreeCollection { get; set; }

class CVariable:
    """docstring for CVariable."""
    def __init__(self, CR2WFILE):
        super(CVariable, self).__init__()
        self.CR2WFILE = CR2WFILE
    def Read(self, f, size):
        self.startpos = f.tell()
        self.classEnd = None
        zero = readSByte(f)
        if zero != 0:
            print("Error")

        self.MoreProps = []
        while True:
            prop = PROPERTY(f, self.CR2WFILE, self)
            if prop.Type == None:
                break
            self.MoreProps.append(prop)
        self.endpos = f.tell()
        bytesread = self.endpos - self.startpos
        if (bytesread > size):
            if (size != 0):
                print("Read bytes not equal to expected bytes. Difference: {bytesread - size}")
        elif (bytesread < size):
            pass

class SMeshChunkPacked():
    def __init__(self, CR2WFILE):
        self.CR2WFILE = CR2WFILE
        self.PROP = False
		# CEnum<EMeshVertexType> VertexType
		# CUInt32 MaterialID
		# CUInt8 NumBonesPerVertex
		# CUInt32 NumVertices
		# CUInt32 NumIndices
		# CUInt32 FirstVertex
		# CUInt32 FirstIndex
		# CEnum<EMeshChunkRenderMask> RenderMask
		# CBool UseForShadowmesh
    def Read(self, f, size):
        self.startpos = f.tell()
        self.classEnd = None
        zero = readSByte(f)
        if zero != 0:
            print("Error")

        self.MoreProps = []
        while True:
            prop = PROPERTY(f, self.CR2WFILE, self)
            if prop.Type == None:
                break
            self.MoreProps.append(prop)
        self.endpos = f.tell()
        bytesread = self.endpos - self.startpos
        if (bytesread > size):
            if (size != 0):
                print("Read bytes not equal to expected bytes. Difference: {bytesread - size}")
        elif (bytesread < size):
            pass

        # create a type manager for each value in SMeshChunkPacked
        # create class for each type
        # add all variable to the MoreProps Array so it functions normally
        #self.PROP = PROPERTY(f, self.CR2WFILE, self)

class CVariantSizeNameType():
    def __init__(self, __CR2WFILE, PROP = None):
        self.__CR2WFILE = __CR2WFILE
        self.PROP = PROP
    def Read(self, f, size):
        varsize = readU32(f)#file.ReadUInt32();
        self.classEnd = varsize
        buffer = f.read(varsize - 4)#file.ReadBytes((int)varsize - 4);
        br = bStream(data = bytearray(buffer))
        nameId = readUShort(br)
        typeId = readUShort(br)

        if (nameId == 0):
            return

        typename = self.__CR2WFILE.CNAMES[typeId].name.value
        varname = self.__CR2WFILE.CNAMES[nameId].name.value
        propstart = PROPSTART_BLANK()
        propstart.size = varsize
        propstart.name = varname
        propstart.type = typename
        self.PROP = PROPERTY(br, self.__CR2WFILE, self, False, propstart)

class CVariantSizeType():
    def __init__(self, __CR2WFILE):
        self.__CR2WFILE = __CR2WFILE
        self.PROP = False
    def Read(self, f, size):
        varsize = readU32(f)#file.ReadUInt32();
        self.classEnd = varsize
        buffer = f.read(varsize - 4)#file.ReadBytes((int)varsize - 4);
        br = bStream(data = bytearray(buffer))
        typeId = readUShort(br)


        typename = self.__CR2WFILE.CNAMES[typeId].name.value
        propstart = PROPSTART_BLANK()
        propstart.size = varsize
        propstart.type = typename
        self.PROP = PROPERTY(br, self.__CR2WFILE, self, False, propstart)

class CArray():
    def __init__(self, __CR2WFILE, array_type):
        self.array_type = array_type
        self.__CR2WFILE = __CR2WFILE
        self.elements = []
    def Read(self, f, size):
        elementcount = readU32(f)
        for _ in range(0, elementcount):
            element = self.array_type(self.__CR2WFILE)
            element.Read(f, 0)
            self.elements.append(element)

class CMaterialInstance():
    def __init__(self, __CR2WFILE):
        self.__CR2WFILE = __CR2WFILE
        self.InstanceParameters = CArray(__CR2WFILE, CVariantSizeNameType) #new CBufferUInt32<CVariantSizeTypeName>(cr2w, this, nameof(variables)) { IsSerialized = true };
    def Read(self, f, size = 0):
        self.InstanceParameters.Read(f, size)

class IAttachment(object):
    """docstring for IAttachment."""
    def __init__(self):
        super(IAttachment, self).__init__()

class CHardAttachment(IAttachment):
    """docstring for CHardAttachment."""
    def __init__(self):
        super(CHardAttachment, self).__init__()

class CLayerGroup(object):
    """docstring for CLayerGroup."""
    def __init__(self):
        #super(CLayerGroup, self).__init__()
        self.name:str = ""  #CString
        self.depotPath:str = ""  #CString
        self.absolutePath:str = ""  #CString
        self.isVisibleOnStart:bool = 0  #CBool
        self.systemGroup:bool = 0  #CBool
        self.hasEmbeddedLayerInfos:bool = 0  #CBool
        self.idHash:int = 0  #CUInt64

    def Read(self, f, CR2WFILE):
        self.worldHandle = HANDLE(f, CR2WFILE, self)
        self.layergrouphandle = HANDLE(f, CR2WFILE, self)

        count = readSByte(f)
        self.ChildrenGroups = []
        for _ in range(0, count):
            self.ChildrenGroups.append(HANDLE(f, CR2WFILE, self))

        count = readSByte(f)
        self.ChildrenInfos = []
        for _ in range(0, count):
            self.ChildrenInfos.append(HANDLE(f, CR2WFILE, self))



class W_CLASS:
    def get_CR2W_version(self):
        return self.__CR2WFILE.HEADER.version
    
    def get_name_prop_string(self):
        entity_name = self.GetVariableByName('name')
        if entity_name:
            return f"{entity_name.String.String} ({self.name})"
        else:
            return self.name

    def GetVariableByName(self, str):
        for item in self.PROPS:
            if item.theName == str:
                return item
        return None

    def __init__(self, f = None, CR2WFILE = None, parent = None, idx = False, **kwargs):
        self.PROPS:list = [],
        self.Type:str = '',
        self.name:str = ''
        self.ChunkIndex:int = idx
        self.__CR2WFILE = CR2WFILE
        if f and not kwargs:
            self.Read(f, CR2WFILE, parent, idx)
        else:
            self.Create(kwargs)

    def Create(self, args):
        for arg in args.items():
            setattr(self, arg[0], arg[1])

    def Read(self, f, CR2WFILE, parent, idx = False):
        if hasattr(parent, 'Handle'):
            idx = readU32Check(f.tell()) -1 #idx = ReadUInt(FTell()) - 1;
            f.seek(parent.exports[idx])#FSeek(CR2WFile[level].exports[idx]);
        #elif idx == False:
            # for idx in range(0, CR2WFILE.maxExport):
            #     if parent.exports[idx] == f.tell():
            #         break
        startofthis = f.tell() - 1 # -1 for zero read before start
        currentClass = CR2WFILE.CR2WExport[idx].name
        self.classEnd = startofthis + CR2WFILE.CR2WExport[idx].dataSize;#local uint64
        self.PROPS = []
        self.propCount  = 0; # local uint
        self.name = CR2WFILE.CR2WExport[idx].name; #local string
        self.Type = self.name #! same as name
        # if self.name == 'CAnimationBufferBitwiseCompressed':
        #     log.debug("CAnimationBufferBitwiseCompressed class")
        # if self.name == "CSkeleton":
        #     log.debug("CSkeleton")
        if self.name == "CHardAttachment":
            log.debug("CHardAttachment")
        # if self.name == "CHardAttachment":
        #     log.debug("CHardAttachment")
        tempClass  = parent.currentClass; # local string


        if CR2WFILE.HEADER.version <= 115: #? WITCHER 2
            startofthis -=1
            self.classEnd -=1
            f.seek(-1, os.SEEK_CUR)
            while True:
                prop = None
                try:
                    prop = PROPERTY(f, CR2WFILE, self)
                except Exception as e:
                    log.warn(f"Witcher 2 Prop Read Error \"{e}\"")
                if prop == None:
                    props_error = f.tell()
                    continue
                if prop.Type == None:
                    props_end = f.tell()
                    break
                self.PROPS.append(prop)
                
            if self.name in Entity_Type_List:
                CR2WFILE.entity_count +=1
                self.isCreatedFromTemplate = False
                self.Template = self.GetVariableByName('template')
                #self.Transform = self.GetVariableByName('transform').EngineTransform
                if self.Template and self.Template.Handles and self.Template.Handles[0].DepotPath:
                    self.isCreatedFromTemplate = True
                self.Components = []
                f.seek(10,1)
                size = self.classEnd - startofthis
                endPos = f.tell()
                bytesleft = size - (endPos - startofthis)
                log.info(self.name)
                if (not self.isCreatedFromTemplate):
                    f.seek(63,1)
                    if bytesleft > 0:
                        testpos = f.tell()
                        elementcount = ReadBit6(f)
                        if elementcount < 300:
                            for item in range(0,elementcount):
                                self.Components.append(readInt32(f))
                        else:
                            log.critical('Waring found too many Components')
                else:
                    log.info(f'Found {self.name} Template')
                    pass #template buffers??
                
                endPos = f.tell()
                bytesleft = size - (endPos - startofthis)
                self.BufferV2 = False
                if (self.isCreatedFromTemplate):
                    f.seek(-10,1)
                    self.BufferV2 = CBufferUInt32(CR2WFILE, SEntityBufferType2)
                    if (bytesleft > 0):
                        self.BufferV2.Read(f, 0)
                        self.BufferV2 = self.BufferV2.elements
                    else:
                        log.warning("unknown CEntity Fileformat.")
                

            elif currentClass == "CMesh": #! for now CMesh is read in dc_mesh
                self.CMesh = CMesh(CR2WFILE)
            elif self.name == "CMaterialInstance":
                f.seek(1, os.SEEK_CUR)
                
                # 'diffusecolor'
                # 'fadeSharpness'
                # 'fogColor'
                #nMatElement = readInt32(f)
                
                MyMaterialInstance = CMaterialInstance(CR2WFILE)
                MyMaterialInstance.Read(f)
                self.CMaterialInstance = MyMaterialInstance

        elif currentClass == "CStorySceneSection":
            while True:
                prop = PROPERTY(f, CR2WFILE, self)
                if prop.Type == None:
                    break
                self.PROPS.append(prop)
            self.sceneEventElements = CArray(CR2WFILE, CVariantSizeType)
            self.sceneEventElements.Read(f, 0)
        elif currentClass == "CMesh":
            while True:
                prop = PROPERTY(f, CR2WFILE, self)
                if prop.Type == None:
                    break
                self.PROPS.append(prop)
            #ReadAllRedVariables
            #REDBuffers
            self.CMesh = CMesh(CR2WFILE)
            self.CMesh.Read(f, 0)
        elif self.name == "CMaterialInstance":
            while True:
                prop = PROPERTY(f, CR2WFILE, self)
                if prop.Type == None:
                    break
                self.PROPS.append(prop)
            MyMaterialInstance = CMaterialInstance(CR2WFILE)
            MyMaterialInstance.Read(f)
            self.CMaterialInstance = MyMaterialInstance
        elif currentClass == "CLayerGroup":
            while True:
                prop = PROPERTY(f, CR2WFILE, self)
                if prop.Type == None:
                    break
                self.PROPS.append(prop)
            #4939667
            #f.seek(2,1);
            #ckae = f.tell()
            self.worldHandle = HANDLE(f, CR2WFILE, self)
            self.layergrouphandle = HANDLE(f, CR2WFILE, self)

            # self.ChildrenGroups = CBufferVLQInt32(CR2WFILE, HANDLE)
            # self.ChildrenGroups.Read(f, 0)
            # self.ChildrenInfos = CBufferVLQInt32(CR2WFILE, HANDLE)
            # self.ChildrenInfos.Read(f, 0)

            count = ReadVLQInt32(f)
            self.ChildrenGroups = []
            for _ in range(0, count):
                self.ChildrenGroups.append(HANDLE(f, CR2WFILE, self))
                if self.ChildrenGroups[-1].Reference > 100000:
                    pass

            count = ReadVLQInt32(f)
            self.ChildrenInfos = []
            for _ in range(0, count):
                self.ChildrenInfos.append(HANDLE(f, CR2WFILE, self))
                if self.ChildrenInfos[-1].Reference > 100000:
                    int1 = readInt32(f)
                    int2 = readInt32(f)
                    int3 = readInt32(f)

            #group = CLayerGroup()
            f.seek(self.classEnd)
            #log.critical('CLayerGroup')
        elif currentClass == "CLayerInfo":
            if idx == 42:
                cae = 5345
            while True:
                prop = PROPERTY(f, CR2WFILE, self)
                if prop.Type == None:
                    break
                self.PROPS.append(prop)
            self.ParentGroup = HANDLE(f, CR2WFILE, self)
            if self.ParentGroup.Reference > 100000:
                print("ParentGroup.Reference > 100000")
            f.seek(self.classEnd)
        elif (CR2WFILE.CR2WExport[idx].dataSize == 5 and readUShortCheck(f.tell()+2) < CR2WFILE.CR2WTable[1].itemCount):
            f.seek(startofthis + 2)#FSeek(startof(this) + 2);
            #STRINGINDEX scnAnimName;
        elif (CR2WFILE.CR2WExport[idx].dataSize > 3):
            dataEnd = f.tell() + CR2WFILE.CR2WExport[idx].dataSize; #local uint64
            idxTotals = 0; #local int
            while (f.tell() < self.classEnd-1 and f.tell() + 4 < FileSize(f) and readU32Check(f, f.tell()) != 1462915651):
                if (detectedProp(f, CR2WFILE, f.tell()) and f.tell()+4 < self.classEnd):
                    start_time = time.time()
                    self.PROPS.append(PROPERTY(f, CR2WFILE, self))
                    time_taken = time.time() - start_time
                    log.debug(' Read PROP in %f seconds.', time.time() - start_time)
                    if time_taken > 0.3:
                        log.warn("time_taken > 0.3")
                    if self.PROPS[-1].dataEnd != f.tell():
                        log.warning(r'dataEnd was not correct '+self.name)
                        f.seek(self.PROPS[-1].dataEnd) # TODO NEEDS MORE TESTING
                    # if len(self.PROPS) == 16 and currentClass == "CClipMap":
                    #     log.critical("CClipMap")
                    self.propCount+=1
                else:
                    if self.name in Entity_Type_List:
                        self.isCreatedFromTemplate = False
                        self.Template = self.GetVariableByName('template')
                        #self.Transform = self.GetVariableByName('transform').EngineTransform
                        if self.Template and self.Template.Handles and self.Template.Handles[0].DepotPath:
                            self.isCreatedFromTemplate = True
                        self.Components = []
                        f.seek(10,1)
                        size = self.classEnd - startofthis
                        endPos = f.tell()
                        bytesleft = size - (endPos - startofthis)
                        log.info(self.name)
                        if (not self.isCreatedFromTemplate):
                            if bytesleft > 0:
                                elementcount = ReadBit6(f)
                                for item in range(0,elementcount):
                                    self.Components.append(readInt32(f))


                        endPos = f.tell()
                        bytesleft = size - (endPos - startofthis)
                        self.BufferV1 = []

                        if (bytesleft > 0):
                            idx = 0
                            canRead = True

                            while canRead:
                                t_buffer = SEntityBufferType1(CR2WFILE, self.BufferV1, str(idx))
                                canRead = t_buffer.CanRead(CR2WFILE, f)
                                if canRead:
                                    t_buffer.Read(f, 0)
                                    self.BufferV1.append(t_buffer)
                                    idx+=1
                        else:
                            log.critical("unknown CEntity Fileformat.") #throw new EndOfStreamException("unknown CEntity Fileformat.");

                        endPos = f.tell()
                        bytesleft = size - (endPos - startofthis)
                        self.BufferV2 = False
                        if (self.isCreatedFromTemplate):
                            self.BufferV2 = CBufferUInt32(CR2WFILE, SEntityBufferType2)
                            if (bytesleft > 0):
                                self.BufferV2.Read(f, 0)
                                self.BufferV2 = self.BufferV2.elements
                            else:
                                log.warning("unknown CEntity Fileformat.")#throw new EndOfStreamException("unknown CEntity Fileformat.");
                    if self.name == "CSkeletalAnimationSetEntry": #TODO FIX ENTRIES
                        #zero = readUShort(f)
                        # f.seek(2,1);
                        # ent_count = readU32(f)
                        # self.entries = []
                        # for _ in range(0, ent_count):
                        #     #elementsize = readU32(f)
                        #     #//var nameId = file.ReadUInt16();
                        #     #typeId = readUShort(f) #file.ReadUInt16();
                        #     #typeName = CR2WFILE.CNAMES[typeId].name.value #cr2w.names[typeId].Str;
                        #     #//var varname = cr2w.strings[nameId].str;

                        #     # var item = CR2WTypeManager.Get().GetByName(typeName, typeName, cr2w, false);
                        #     # if (item == null)
                        #     #     item = new CVector(cr2w);


                        #     # item.Read(file, elementsize);
                        #     # item.Type = typeName;
                        #     # item.Name = typeName;
                        #     self.entries.append(PROPERTY(f,CR2WFILE,self, True));
                        #     f.seek(-2,1);
                        f.seek(self.classEnd)
                    elif self.name == "CSkeleton":
                        for item in self.PROPS:
                            if item.theName == "bones":
                                bonecount = len(item.More)
                                break
                        self.rigData = CCompressedBuffer(f, CR2WFILE, self, Name = "rigData")
                        self.rigData.Read(f, bonecount * 48, bonecount)
                        #f.seek(self.classEnd)
                    elif self.name == "CFoliageResource":
                        self.Trees = CBufferVLQInt32(CR2WFILE, SFoliageResourceData)
                        self.Trees.Read(f, 0)
                        self.Grasses = CBufferVLQInt32(CR2WFILE, SFoliageResourceData)
                        self.Grasses.Read(f, 0)
                    elif self.name == "CSectorData":
                        dict = Cr2wResourceManager().Get()
                        f.seek(-1,1)
                        ukn1 = readU64(f) #46871095541760

                        #RESOURCE
                        count = ReadBit6(f)
                        self.Resources = []
                        for _ in range(0, count):
                            self.Resources.append(CSectorDataResource(f, CR2WFILE, self))

                        #OBJECTS
                        count = ReadBit6(f)
                        self.Objects = []
                        for _ in range(0, count):
                            self.Objects.append(CSectorDataObject(f, CR2WFILE, self))

                        pos = f.tell()
                        self.blocksize = ReadVLQInt32(f)

                        # #BLOCKDATA
                        self.BlockData = []
                        idx = 0
                        for curobj in self.Objects:
                            curoffset = curobj.offset
                            leng = 0 #ulong
                            if (idx < len(self.Objects) - 1):
                                nextobj = self.Objects[idx + 1]
                                nextoffset = nextobj.offset; #ulong
                                leng = nextoffset - curoffset
                            else:
                                leng = self.blocksize - curoffset
                            self.BlockData.append(SBlockData(f, leng, curobj.type))
                            idx += 1
                        # for _ in range(0, count):
                        #     self.BlockData.append(SBlockData(f))

                        f.seek(self.classEnd)
                    elif self.name == "CGameWorld":
                        f.seek(2,1) # this reads zero "type" at the end of normal PROPS
                        self.Firstlayer = HANDLE(f, CR2WFILE, self)
                    elif self.name == "CLayerGroup":
                        #f.seek(2,1);
                        self.worldHandle = HANDLE(f, CR2WFILE, self)
                        self.layergrouphandle = HANDLE(f, CR2WFILE, self)

                        count = readSByte(f)
                        self.ChildrenGroups = []
                        for _ in range(0, count):
                            self.ChildrenGroups.append(HANDLE(f, CR2WFILE, self))

                        count = readSByte(f)
                        self.ChildrenInfos = []
                        for _ in range(0, count):
                            self.ChildrenInfos.append(HANDLE(f, CR2WFILE, self))

                        f.seek(self.classEnd)
                    elif self.name == "CLayerInfo":
                        f.seek(2,1)
                        endpos = f.tell()
                        bytesread = endpos - startofthis
                        self.ParentGroup = HANDLE(f, CR2WFILE, self)
                        f.seek(self.classEnd)
                    else:
                        f.seek(self.classEnd)
                        #f.seek(1,1);
            if (hasattr(parent, 'Handle')):
                f.seek(startofthis + 4)
            if self.name == "CSkeletalAnimation" and len(self.PROPS):
                parent.animCount+=1
                log.info(str(parent.animCount)+": "+self.GetVariableByName("name").Index.String)
        else:
            log.warning("dummy readUByte")
            dummy = readUByte(f)
        currentClass = tempClass
        #self.refChunk = self
        self.ChunkIndex = idx

class CR2WProperty:
    def __init__(self, f = None, **kwargs):
        if f and not kwargs:
            self.Read(f)
        else:
            self.Create(kwargs)

    def Read(self, f):
        self.className = readUShort(f)
        self.classFlags = readUShort(f)
        self.propertyName = readUShort(f)
        self.propertyFlags = readUShort(f)
        self.hash = readU64(f)

    def Create(self, args):
        self.className = 0
        self.classFlags = 0
        self.propertyName = 0
        self.propertyFlags = 0
        self.hash = 0
        for arg in args.items():
            setattr(self, arg[0], arg[1])

    def Write(self, f):
        raise NotImplementedError('')

class CR2WBuffer:
    def __init__(self,f = None, **kwargs):
        if f and not kwargs:
            self.Read(f)
        else:
            self.Create(kwargs)

    def Read(self, f):
        self.flags = readU32(f)
        self.index = readU32(f)
        self.offset = readU32(f)
        self.diskSize = readU32(f)
        self.memSize = readU32(f)
        self.crc32 = readU32(f)

    def Create(self, args):
        self.flags = 0
        self.index = 0
        self.offset = 0
        self.diskSize = 0
        self.memSize = 0
        self.crc32 = 0
        for arg in args.items():
            setattr(self, arg[0], arg[1])

    def Write(self, f):
        raise NotImplementedError('')

class CR2WExport_Witcher2:
    def __init__(self,f, CR2WFILE):
        #self.typeNameIndex = readUShort(f)
        self.className = STRINGINDEX(f, CR2WFILE, self)#CR2WFILE.STRINGS[self.typeNameIndex - 1]
        #self.objectFlags = readUShort(f)# ushort objectFlags;
        self.parentID = readU32(f) # uint parentID
        self.dataSize = readU32(f) # uint dataSize
        self.dataOffset = readU32(f) # uint dataOffset
        self.objectFlags = readU32(f) # uint template

        self.Unknown5 = readU32(f)
        if CR2WFILE.HEADER.version < 102:
            self.Link = None
        else:
            self.Link = STRING(f).String

        self.name = CR2WFILE.CNAMES[self.className.Index].name.value # local string name <hidden=true> = CR2WFile[level].NAMES.Name[className.Index].name;

        # f.seek(self.dataOffset + CR2WFILE.start) # FSeek(dataOffset + start);
        # # struct { FSkip(dataSize); } Data;
        # f.seek(self.dataSize, 1)



class CR2WExport:
    def __init__(self, f = None, CR2WFILE = None, **kwargs):
        self.className = None
        self.objectFlags = None
        self.parentID = None
        self.dataSize = None
        self.dataOffset = None
        self.template = None
        self.crc32 =None
        self.name = None
        if f:
            self._CR2WFILE = CR2WFILE
            self.ReadCR2WExport(f)
        else:
            self.Create(kwargs)

    def Create(self, args):
        self.className = None
        self.objectFlags = None
        self.parentID = None
        self.dataSize = None
        self.dataOffset = None
        self.template = None
        self.crc32 =None
        self.name = None
        for arg in args.items():
            setattr(self, arg[0], arg[1])

    def ReadCR2WExport(self, f):
        #string ReadCR2WEXPORT (CR2WEXPORT &input) { return input.name; }
        self.className = STRINGINDEX(f, self._CR2WFILE, self)# STRINGINDEX className;
        self.objectFlags = readUShort(f)# ushort objectFlags;
        self.parentID = readU32(f) # uint parentID
        self.dataSize = readU32(f) # uint dataSize
        self.dataOffset = readU32(f) # uint dataOffset
        self.template = readU32(f) # uint template
        startofcrc32 = f.tell()
        self.crc32 = readU32(f); # uint crc32;
        self.name = self._CR2WFILE.CNAMES[self.className.Index].name.value # local string name <hidden=true> = CR2WFile[level].NAMES.Name[className.Index].name;
        f.seek(self.dataOffset + self._CR2WFILE.start) # FSeek(dataOffset + start);
        # struct { FSkip(dataSize); } Data;
        f.seek(self.dataSize, 1)
        f.seek(startofcrc32 + 4)# FSeek(startof(crc32)+4);

class CR2WImport:
    def __init__(self, f = None, CR2WFILE = None, **kwargs):
        self.depotPath = None
        self.path = None
        self.className = None
        self.flags = None
        if f and not kwargs:
            self._CR2WFILE = CR2WFILE
            self.ReadCR2WIMPORT(f)
        else:
            self.Create(kwargs)

    def ReadCR2WIMPORT(self, f):
        startofdepotPath = f.tell()
        self.depotPath = readU32(f)
        f.seek(self.depotPath +  self._CR2WFILE.CR2WTable[0].offset +  self._CR2WFILE.start)
        self.path = getString(f)#string path <open=suppress>;
        f.seek(startofdepotPath+4)
        self.className = readUShort(f)
        self.flags = readUShort(f)
        #string ReadCR2WIMPORT (CR2WIMPORT &input) { return input.path; }

    def WriteCR2WIMPORT():
        #void WriteCR2WIMPORT (CR2WIMPORT &f, string s ) { forceWriteString(startof(f.path), sizeof(f.path), s); }
        pass

    def Create(self, args):
        self.depotPath = None
        self.path:str = None
        self.className = None
        self.flags = None
        for arg in args.items():
            setattr(self, arg[0], arg[1])

def getCR2WTABLEName(index, version):
    if version <= 115:
        if index == 0: return "name"
        if index == 1: return "object"
        if index == 2: return "link"
        if index == 3: return "dependency"
    else:
        if index == 0: return "Strings"
        if index == 1: return "Enums"
        if index == 2: return "CR2WImport"
        if index == 3: return "CR2WProperty"
        if index == 4: return "CR2WExport"
        if index == 5: return "CR2WBuffer"
    return "Unknown"

class CR2WTABLE:
    def __init__(self, index, f, version):
        #Index = 0 #local uint Index <hidden=true> = CR2WTableIdx; CR2WTableIdx++;
        self.tableName = getCR2WTABLEName(index, version)
        self.offset = readU32(f) #uint offset;
        self.itemCount = readU32(f) #uint itemCount;

        if version <= 115:
            pass
        else:
            self.crc32 = readU32(f) #uint crc32;

class CR2W_header:
    def __init__(self,f = None, **kwargs):
        if f and not kwargs:
            self.Read(f)
        else:
            self.Create(kwargs)

    def Read(self, f = None):
        self.magic: np.uint = readU32(f)
        self.version: np.uint = readU32(f) # witcher3 = 162

        if (self.version <= 115):# witcher2
            self.flags: np.uint = readU32(f)
            log.error("w2 header error")
        else:
            self.flags: np.uint = readU32(f)
            self.timestamp: np.uint64 = readU64(f)
            self.buildVersion: np.uint = readU32(f)
            self.fileSize: np.uint = readU32(f)
            self.bufferSize: np.uint = readU32(f)
            self.CRC32: np.uint = readU32(f)
            self.numChunks: np.uint = readU32(f)

    def Create(self, args):
        self.magic: np.uint = 0
        self.version: np.uint = 0
        self.flags: np.uint = 0
        self.timestamp: np.uint64 = 0
        self.buildVersion: np.uint = 0
        self.fileSize:np.uint = 0
        self.bufferSize:np.uint = 0
        self.CRC32:np.uint = 0
        self.numChunks:np.uint = 0
        for arg in args.items():
            setattr(self, arg[0], arg[1])

class EStringTableMod(Enum):
    None_ = 0
    SkipType = 1
    SkipName = 2
    SkipNameAndType = 3
    TypeFirst = 4

class W2CNAME:
    def __init__(self,str):
        self.value = str
class W2NAME:
    def __init__(self,str):
        self.name = W2CNAME(str)

class CR2W:
    def __init__(self, f = None, anim_name = None, do_read_chunks = True):
        self.do_read_chunks = do_read_chunks
        #global variables to use
        self.gName:np.string_ = ""
        self.gType:np.string_ = ""
        self.childrendict = {}

        if f:
            self.Read(f, anim_name)
        else:
            self.Create()

    def Create(self):
        self.start:np.uint = 0
        self.fileName:np.string_ = None
        self.HEADER:CR2W_header = CR2W_header()
        table_range = 10

    def GenerateStringtable(self):
        newstringtable = {}


        (nameslist, importslist) = self.GenerateStringtableInner()
        stringlist:List[str] = nameslist
        newstrings:bytearray = []

        return (newstringtable, newstrings, nameslist, importslist)

    def GenerateStringtableInner(self):
        dbg_trace:List[str]= []
        newnameslist = {"":""}
        newimportslist: List[CR2WImport] = []
        newsoftlist: List[CR2WImport] = []
        guidlist = []
        chunkguidlist = []
        c: W_CLASS = None

        def LoopWrapper(item1: EStringTableMod, item2: W_CLASS):
            dbg_trace.append(f"{item2.name}[{item2.Type}] - {item1.name}")
            self.AddStrings(item1, item2)

        for c in self.CHUNKS.CHUNKS:
            LoopWrapper(EStringTableMod.SkipName, c)


        return (newnameslist, newimportslist)

    def AddStrings(self, item1: EStringTableMod, item2: W_CLASS):
        var = item2
        pass

    def Write(self, f):
        nn: List[CNAME] = self.CNAMES.copy()

        StringDictionary = {}

        newstrings:List[np.byte]
        nameslist:str = []
        importslist:List[CR2WImport] = []

        (StringDictionary, newstrings, nameslist, importslist) = self.GenerateStringtable()

    def Read(self, f = None, anim_name = None):
        #!debug
        self.entity_count = 0
        
        #!
        self.LocalizedStrings:List[LocalizedString] = []
        self.fileName:np.string_ = f.name
        start:np.uint = f.tell()
        self.start = start
        self.HEADER:CR2W_header = CR2W_header(f)
        table_range:np.uint = 10
        #if self.HEADER.version == 112: table_range = 4 !# seems to be wrong for some 112 files


        if (self.HEADER.version <= 115): #? WITCHER 2
            self.CR2WTable = []
            for i in range(0, table_range):
                self.CR2WTable.append(CR2WTABLE(i, f, self.HEADER.version))

            self.maxExport = 0
            if (self.CR2WTable[4].itemCount > self.maxExport):
                self.maxExport = self.CR2WTable[4].itemCount

            if (self.CR2WTable[0].offset > 0):
                self.STRINGS = []
                f.seek(self.CR2WTable[0].offset + start)
                for _ in range(0, self.CR2WTable[0].itemCount):
                    # Array = 3, // @
                    # Pointer = 4, // *
                    # Handle = 5, // #
                    # SoftHandle = 6, // ~
                    
                    Str = STRING(f).String
                    if "@*" in Str:
                        #Str = Str.replace("@*", "array:2,0,ptr:")
                        Str = Str.replace("@*", "array:2,0,handle:")
                    elif "@#" in Str:
                        Str = Str.replace('@#', 'array:2,0,handle:')
                    elif "@" in Str:
                        Str = Str.replace("@", "array:2,0,")
                    elif "*" in Str:
                        #Str = Str.replace("*", "ptr:")
                        Str = Str.replace("*", "handle:") ##TODO check why, pointers are pointing to depot paths etc
                    elif "#" in Str:
                        Str = Str.replace("#", "handle:")
                    self.STRINGS.append(Str)
            self.CNAMES = []
            self.CNAMES.append(W2NAME(""))
            for _str in self.STRINGS:
                self.CNAMES.append(W2NAME(_str))

            if (self.CR2WTable[1].offset > 0) :
                self.CR2WExport = []
                f.seek(self.CR2WTable[1].offset + start)
                for _ in range(0, self.CR2WTable[1].itemCount):
                    self.CR2WExport.append(CR2WExport_Witcher2(f, self))

            ## IMPORTS
            if (self.CR2WTable[2].offset > 0) :
                self.CR2WImport = []
                f.seek(self.CR2WTable[2].offset + start)
                for _ in range(0, self.CR2WTable[2].itemCount):
                    lent = ReadBit6(f)
                    #the_path = getStringOfLen(f, readSByte(f)-1)
                    the_path = getStringOfLen(f, lent-1)
                    
                    myImport = CR2WImport(
                        path = the_path,
                        className = readUShort(f),
                        flags = readUShort(f),
                    )
                    self.CR2WImport.append(myImport)
            
            self.CR2WTable[4] = self.CR2WTable[1]

            self.CHUNKS = DATA(f, self, anim_name)

        else:

            self.CR2WTable = []
            for i in range(0, table_range):
                self.CR2WTable.append(CR2WTABLE(i, f, self.HEADER.version))

            self.maxExport = 0
            if (self.CR2WTable[4].itemCount > self.maxExport):
                self.maxExport = self.CR2WTable[4].itemCount

            if (self.CR2WTable[0].offset > 0):
                self.STRINGS = []
                f.seek(self.CR2WTable[0].offset + start)
                for _ in range(0, self.CR2WTable[1].itemCount):
                    self.STRINGS.append(getString(f))

            if (self.CR2WTable[1].offset > 0):
                self.CNAMES = []
                f.seek(self.CR2WTable[1].offset + start)
                NameCount = self.CR2WTable[1].itemCount
                for _ in range(0, NameCount):
                    self.CNAMES.append(NAME(f, self))

            if (self.CR2WTable[2].offset > 0) :
                self.CR2WImport = []
                f.seek(self.CR2WTable[2].offset + start)
                for _ in range(0, self.CR2WTable[2].itemCount):
                    self.CR2WImport.append(CR2WImport(f, self))

            if (self.CR2WTable[3].offset > 0) :
                self.CR2W_Property = []
                f.seek(self.CR2WTable[3].offset + start)
                for _ in range(0, self.CR2WTable[3].itemCount):
                    self.CR2W_Property.append(CR2WProperty(f))

            if (self.CR2WTable[4].offset > 0) :
                self.CR2WExport = []
                f.seek(self.CR2WTable[4].offset + start)
                for _ in range(0, self.CR2WTable[4].itemCount):
                    self.CR2WExport.append(CR2WExport(f, self))
            if (self.CR2WTable[5].offset > 0) :
                self.CR2WBuffer = []
                self.BufferData = []
                f.seek(self.CR2WTable[5].offset + start)
                for idx in range(0, self.CR2WTable[5].itemCount):
                    self.CR2WBuffer.append(CR2WBuffer(f))
                    pos = f.tell()
                    f.seek(self.CR2WBuffer[idx].offset + start, os.SEEK_SET)
                    data = f.read(self.CR2WBuffer[idx].memSize)
                    self.BufferData.append(data)
                    f.seek(pos, os.SEEK_SET)
            if self.do_read_chunks:
                self.CHUNKS = DATA(f, self, anim_name)

    ###################

    #      JSON       #

    ###################
    def _createJsonHandle(self, _propJson, the_handle):
        _propJson._vars['_chunkHandle'] = CR2WJsonScalar(_type = 'bool', _value = the_handle.ChunkHandle)
        if the_handle.ChunkHandle:
            try:
                _propJson._vars['_reference'] = CR2WJsonScalar(_type = 'string', _value = self.CR2WExport[the_handle.Reference].name+ ' #'+str(the_handle.Reference))
            except:
                _propJson._vars['_reference'] = None
                log.critical('Problem Finding Chunk Reference')
        else:
            _propJson._vars['_className'] = CR2WJsonScalar(_type = 'string', _value = the_handle.ClassName)
            _propJson._vars['_depotPath'] = CR2WJsonScalar(_type = 'string', _value = the_handle.DepotPath)
            _propJson._vars['_flags'] = CR2WJsonScalar(_type = 'uint16', _value = the_handle.Flags)
        return _propJson

    def WalkNode(self, prop):
        # if type(prop) == CMatrix4x4 or prop.__class__.__name__ == "CMatrix4x4":
        #     pass
        if prop.__class__.__name__ in IREDPrimitive:
            return CR2WJsonScalar(_type = prop.type, _value = prop.val)
        elif type(prop) == PROPERTY:
            if prop.theName == 'chunksBuffer':
                ckae =123
            #TODO REMOVE
            if hasattr(prop, "PROPS"):
                prop.PROPS = prop.PROPS
            elif hasattr(prop, "MoreProps"):
                prop.PROPS = prop.MoreProps
            elif hasattr(prop, "More"):
                prop.PROPS = prop.More
            #!TODO REMOVE
            if prop.theType in v_types:
                try:
                    return CR2WJsonScalar(_type = prop.theType, _value = prop.Value)
                except Exception as e:
                    return CR2WJsonScalar(_type = prop.theType, _value = prop.String.String)
            elif prop.theType in Enums.Enum_Types or prop.theType == "CName":
                return CR2WJsonScalar(_type = prop.theType, _value = prop.Index.String)
            elif prop.theType.startswith('ptr:'):
                ptrMap = CR2WJsonMap(_type = prop.theType)
                ref = self.CR2WExport[prop.Value-1].name+ ' #'+str(prop.Value-1)
                ptrMap._vars['_reference'] = CR2WJsonScalar(_type = 'string', _value = ref)
                return ptrMap
            elif prop.theType.startswith('handle:'):
                _propJson = CR2WJsonMap(_type = prop.theType)
                the_handle = prop.Handles[0]
                return self._createJsonHandle(_propJson, the_handle)
            elif prop.theType.startswith('array:'):
                array = CR2WJsonArray(_type = prop.theType)

                ############################# ##TODO GET RID OF THIS
                if hasattr(prop, 'chunks'):
                    prop.elements = prop.chunks.elements
                elif hasattr(prop, 'Handles'):
                    prop.elements = prop.Handles
                elif hasattr(prop, 'PROPS'):
                    prop.elements = prop.PROPS
                ###########################

                if hasattr(prop, 'elements'):
                    for element in prop.elements:
                        _elJson = self.WalkNode(element)
                        if _elJson._type == 'ELEMENT':
                            _elJson._type = prop.theType.split(',')[-1]
                        array._elements.append(_elJson)
                return array
            elif prop.theType == "DeferredDataBuffer":
                dbuf = CR2WJsonMap(_type = prop.theType)
                dbuf._vars['Bufferdata'] = CR2WJsonScalar(_type = "Int16", _value = prop.ValueA)
                return dbuf
            elif prop.theType == "CDateTime":
                CR2WJsonScalar(_type = "CDateTime", _value = prop.DateTime.String.replace('/','-').replace(' ','T'))
            else:
                try:
                    _propJson = CR2WJsonMap(_type = prop.theType)
                    prop:PROPERTY
                    for prop in prop.PROPS:
                        _propJsonVar = self.WalkNode(prop)
                        _propJson._vars[prop.theName] = _propJsonVar
                    return _propJson
                except Exception as e:
                    log.critical('UNKNOWN PROP')
                    return None

        elif type(prop) == CPaddedBuffer: # primities
            array = CR2WJsonArray(_type = prop.theType)
            array._bufferPadding = prop.padding
            for element in prop.elements:
                _elJson = self.WalkNode(element)
                array._elements.append(_elJson)
            return array
        elif hasattr(prop, 'elements'):
            array = CR2WJsonArray(_type = prop.theType)

            for element in prop.elements:
                _elJson = self.WalkNode(element)
                array._elements.append(_elJson)
            return array
        elif type(prop) == STRING: # primities
            return CR2WJsonScalar(_type = 'String', _value = prop.String)
        elif type(prop) == HANDLE: # primities
            _propJson = CR2WJsonMap(_type = prop.theType)
            the_handle = prop
            return self._createJsonHandle(_propJson, the_handle)
        elif type(prop) == CMatrix4x4 or prop.__class__.__name__ == "CMatrix4x4":
            _propJson = CR2WJsonMap(_type = prop.__class__.__name__)
            for idx, field in enumerate(prop.fields):
                _propJson._vars[prop.fieldNames[idx]] = CR2WJsonScalar(_type = "Float", _value = float(field))
            return _propJson
        elif type(prop) == CNAME_INDEX: # primities
            return CR2WJsonScalar(_type = "CName", _value = prop.value.name.value)
        elif type(prop) == CVariantSizeNameType:
            _propJson = CR2WJsonMap(_type = prop.__class__.__name__)
            _propJsonVar = self.WalkNode(prop.PROP)
            _propJson._vars["_variant"] = _propJsonVar
            _propJson._vars["_name"] = CR2WJsonScalar(_type = "string", _value = prop.PROP.theName)
            return _propJson
        elif prop.__class__.__name__.startswith('CVariant'):
            raise NotImplementedError('new variaint class')
        else:
            if hasattr(prop, "PROPS"):
                PROPS = prop.PROPS
            elif hasattr(prop, "MoreProps"):
                PROPS = prop.MoreProps
            elif hasattr(prop, "More"):
                PROPS = prop.More
            elif hasattr(prop, "PROP"):
                PROPS = [prop.PROP]
            _propJson = CR2WJsonMap(_type = prop.__class__.__name__)
            prop:PROPERTY
            for prop in PROPS:
                _propJsonVar = self.WalkNode(prop)
                _propJson._vars[prop.theName] = _propJsonVar
            return _propJson
        return None

    def GetJson(self):
        jsonCR2W = CR2WJsonData(create = True)
        for _import in self.CR2WImport:
            _dict = {
                "_className": _import.className if type(_import.className) == str else self.CNAMES[_import.className].name.value,
                "_depotPath": _import.path,
                "_flags": _import.flags
            }
            jsonCR2W._imports.append(_dict)
        for _properties in self.CR2W_Property:
            jsonCR2W._properties.append({'Property':_properties})
        for idx, _buffer in enumerate(self.CR2WBuffer):
            jsonCR2W._buffers.append({'Buffer':_buffer, 'Data': base64.b64encode(self.BufferData[idx]).decode('utf-8')})

        jsonCR2W._embedded = [] #!TODO
        for chunk in self.CHUNKS.CHUNKS:
            _export = self.CR2WExport[chunk.ChunkIndex]
            _cmap = CR2WJsonChunkMap(_type = chunk.Type)
            _cmap._flags = _export.objectFlags
            _cmap._key = chunk.name+' #'+str(chunk.ChunkIndex)

            def findParent(chunk, cr2w):
                CHUNKS = cr2w.CHUNKS.CHUNKS
                for key, value in cr2w.childrendict.items():
                    if chunk.ChunkIndex in value:
                        res = CHUNKS[int(key)].Type+" #"+str(key)
                        return res
                return ''

            _cmap._parentKey = findParent(chunk, self)

            if hasattr(chunk, 'CMesh'):
                members = [attr for attr in dir(chunk.CMesh) if not callable(getattr(chunk.CMesh, attr)) and not attr.startswith("_")]
                for attr in members:
                    chunk.PROPS.append(getattr(chunk.CMesh, attr))
            if hasattr(chunk, "PROPS"):
                PROPS = chunk.PROPS
            elif hasattr(chunk, "MoreProps"):
                PROPS = chunk.MoreProps
            elif hasattr(chunk, "More"):
                PROPS = chunk.More
            prop:PROPERTY
            for prop in PROPS:
                _propJson = self.WalkNode(prop)
                _cmap._vars[prop.theName] = _propJson

            ##! HACK FIX HOW THIS WORKS
            if hasattr(chunk, 'CMaterialInstance'):
                array = CR2WJsonArray(_type = "array:0,0,CVariantSizeNameType")

                for element in chunk.CMaterialInstance.InstanceParameters.elements:
                    _elJson = self.WalkNode(element)
                    array._elements.append(_elJson)
                _cmap._vars['InstanceParameters'] = array
                # for prop_el in chunk.InstanceParameters.elements:
                #     chunk.PROPS.append(prop_el)

            jsonCR2W._chunks[_cmap._key] = _cmap
        return jsonCR2W

def getCR2W(f, anim_name = None, do_read_chunks = True):
    return CR2W(f, anim_name, do_read_chunks)


