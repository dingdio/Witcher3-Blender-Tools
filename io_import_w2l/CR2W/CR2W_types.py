import csv
import functools
import os
from pathlib import Path
import time
from CR2W.Types.CMesh import CMesh

from CR2W.Types.VariousTypes import CNAME, NAME, CBufferVLQInt32, CColor

from .bStream import *
from .setup_logging import *
log = logging.getLogger(__name__)

from CR2W.CR2W_helpers import Enums
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
                    "W3ReplacerCiri"]




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
    return CLASS(f, CR2WFILE, self, idx)

class DATA:
    def __init__(self, f, CR2WFILE, anim_name):
        self.exports=[]
        self.sizes=[]
        self.CHUNKS=[]
        self.animCount = 0
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
            #self.Class = CLASS(f, CR2WFILE, self)
            start_time = time.time()
            self.CHUNKS.append(CLASS(f, CR2WFILE, self, i))
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
                print("cake")
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
                self.String = CR2WFILE.CNAMES[self.Index].name.value;
        except IndexError:
            pass #couldn't find name index

        try:
            if (self.Index > 0 and not hasattr(parent, 'dataType') and hasattr(CR2WFILE, 'CR2WImport') and CR2WFILE.CR2WImport[self.Index-1].path):
                self.Path = CR2WFILE.CR2WImport[self.Index-1].path;
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
        f.seek(f.tell() + size - 4);

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
        self.strIdx = STRINGINDEX(f, CR2WFILE, self) #<name="String Index">;
        if self.strIdx.Index > 0:
            self.dataType = STRINGINDEX(f, CR2WFILE, self) #<name="Data Type">;
            self.name = CR2WFILE.CNAMES[self.strIdx.Index].name.value; #string
            self.type = CR2WFILE.CNAMES[self.dataType.Index].name.value; #string

            if ("rRef:" in self.type and (readU32Check(f.tell()-8) == 10)):
                self.size = 2; #local int
            elif (f.tell() + 4 < FileSize(f) and readU32Check(f, f.tell()) < FileSize(f) - f.tell() + 2):
                self.size = readU32(f) #uint32
            else:
                self.size = 4; # local ushort

            if (self.size > 4):
                startofData_Bytes = f.tell()
                self.Data = Data_Bytes(f, self.size)
                f.seek(startofData_Bytes);
        else:
            self.dataType = None
            self.name = None
            self.type = None
            

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
    def __init__(self, f):
        self.Value = readU64(f) #uint64;
        self.String = CDate2String(self.Value) #local string String = CDate2String(Value);

class STRINGANSI:
    def __init__(self, f):
        len = readUChar(f)
        self.isUTF = False
        if (len >= 128):
            len = len - 128;
            self.isUTF = True;
            len = len*2;

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
    def Read(self, f, CR2WFILE):
        self.val = str(readU32(f)) #! temp
        return self

class STRING:
    def __init__(self, f):
        startofString = f.tell()
        strLen = readUChar(f) #uchar
        len = strLen
        actualLen = 0 #local uint
        flag = 0 #local uint
        maxSize = 0; #local uint
        #extraLength = readUChar(f) #uchar
        self.isUTF = False
        #f.seek(-1, 1) # FSkip(-1);
        if(strLen >= 128): #128??
            len = len - 128
            if (len >= 64):
                len = len - 64
                len = readUChar(f)*64 + len
            self.String = f.read(len).decode('utf-8')#getStringOfLen(f, len)
            #skipPadding(f)
            # if (strLen >= 192):
            #     f.seek(1,1); flag = 1;
            #     actualLen = (strLen - 128) + 64 * (extraLength-1);
            # else:
            #     actualLen = strLen - 128;
            # if(actualLen > 0):
            #     self.String = getStringOfLen(f, actualLen)
            #     # self.String = []
            #     # for _ in range(0, actualLen):
            #     #     self.String.append(readUChar(f)) #char String[actualLen]
            #skipPadding(f)
            # maxSize = f.tell() - startofString;
            # f.seek(startofString + actualLen);
        else:
            self.isUTF = True;

            if (len >= 64):
                len = len - 64
                len = readUChar(f)*64 + len
            len = len*2
            self.String = f.read(len).decode('utf-16')
            
            # self.String = getStringOfLen(f, strLen)
            # self.String = self.String[0:-1] #remove \x00
            #skipPadding(f)
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
        ElementIdx  = parent.ElementCounter; #local uint
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
                if (prevProp != "metalLevelsOut" and detectedProp(f, CR2WFILE, f.tell()) and CR2WFILE.gName != firstProp) : #multilayer_layers should end at metalLevelsOut
                    Sub_Element_Count += 1
                    if (firstProp == ""):
                        firstProp = CR2WFILE.gName;

                    #setColor();
                    if (CR2WFILE.gName == "v" and CR2WFILE.gType == "[3]Float") :
                        More =PROPERTY(f,CR2WFILE, self)#struct PROPERTY More <open=true>;  FSkip(-1); BLANK blank;
                    else:
                        More =PROPERTY(f,CR2WFILE, self)#struct PROPERTY More;  #//sub property
                    self.MoreProps.append(More)
                    if (self.elementName == "" and exists(More, "Index") and (exists(More, "Type.type") and More.Type.type == "CName")):
                        try:
                            self.elementName = More.Index.String#More.Index[0].String;
                        except IndexError:
                            pass
                    if (CR2WFILE.gName == "material" and title == "" and exists(More , "Path.Path")):
                        title = More.Path.Path;
                    elif (exists(More, "Type") and title == "" and not doesExist(parent.Type.type, "layer")):
                        if (More.Type.name == "id" or endsWith(More.Type.name, "Id") or endsWith(More.Type.type, "Id")):
                            if (exists(More, "Value")):
                                #SPrintf(title, "%s = %Ld", More.Type.name, More.Value);
                                title = "%s = %Ld" % (More.Type.name, More.Value)
                            elif (exists(More, "More[0].Value")):
                                #SPrintf(title, "%s = %Ld", More.Type.name, More.More[0].Value);
                                title = "%s = %Ld" % (More.Type.name, More.More[0].Value)
                        elif (doesExist(More.Type.type, "Ref") and exists(More.Path)):
                            title = More.Path.Path;
                    if (not doesExist(CR2WFILE.gType, "loat") and not doesExist(CR2WFILE.gType, "Uint8")):
                        pass #SetBackColor(cNone);

                    if (parent.theType == "meshLocalMaterialHeader" and CR2WFILE.gName == "size"):
                        parent.ElementCounter+=1;
                        break;
                else:
                    if (parent.lastProp == ""):
                        parent.lastProp = prevProp;

                    prevProp = CR2WFILE.gName;
                    parent.ElementCounter+=1
                    break;
                prevProp = CR2WFILE.gName;
            else:
                f.seek(1,1)#FSkip(1);

class Cr2wResourceManager:
    resourceManager = None
    def __init__(self):
        
        fileDir = os.path.dirname(os.path.realpath(__file__))
        filename = os.path.join(fileDir, "pathhashes.csv")
        self.pathashespath = filename
        #self.HashdumpDict = {}
        reader = csv.DictReader(open(self.pathashespath))
        
        self.HashdumpDict = {}
        for row in reader:
            self.HashdumpDict[row["HashInt"]] = row["Path"]
    @staticmethod
    def Get():
        if (Cr2wResourceManager.resourceManager == None):
            Cr2wResourceManager.resourceManager = Cr2wResourceManager();
        return Cr2wResourceManager.resourceManager;

class CSectorDataResource:
    def __init__(self, f, CR2WFILE, parent):
        self.box0 = readFloat(f)
        self.box1 = readFloat(f)
        self.box2 = readFloat(f)
        self.box3 = readFloat(f)
        self.box4 = readFloat(f)
        self.box5 = readFloat(f)
        self.hashint = readU64(f);
        if self.hashint == 0:
            self.pathHash = 0
        else:
            resoruce = Cr2wResourceManager.Get()
            if str(self.hashint) in resoruce.HashdumpDict:
                self.pathHash = resoruce.HashdumpDict[str(self.hashint)]
            else:
                log.debug("panic")
                self.pathHash = self.hashint;
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
    def __init__(self, f, size, packedObjectType):
        startp = f.tell();

        #base.Read(file, size);
        self.meshIndex = readUShort(f) #CUInt16
        self.padding = readUShort(f) #CUInt16
        self.collisionMask = readU64(f) #CUInt64
        self.collisionGroup = readU64(f) #CUInt64

        endp = f.tell();
        read = endp - startp;
        if (read < size):
            f.seek(size - read, 1)#unk1.Read(file, size - (uint)read);
        elif (read > size):
            log.error("ERROR READING SBlockDataMeshObject")

class SBlockDataDimmer(object):
    """docstring for SBlockDataDimmer."""
    def __init__(self, f, size, packedObjectType):
        startp = f.tell()
        #self.meshIndex = readUShort(f) #CUInt16

        self.ambienLevel = readFloat(f)
        self.marginFactor = readFloat(f)
        self.dimmerType = readUByte(f)
        self.paddin1 = readUByte(f)
        self.paddin2 = readUShort(f)

        endp = f.tell()
        read = endp - startp
        if (read < size):
            f.seek(size - read, 1)#unk1.Read(file, size - (uint)read);
        elif (read > size):
            log.error("ERROR READING SBlockDataDimmer")

class SBlockDataMeshObject:
    def __init__(self, f, size, packedObjectType):
        startp = f.tell();

        #base.Read(file, size);
        self.meshIndex = readUShort(f) # CUInt16
        self.forceAutoHide = readUShort(f) # CUInt16
        self.lightChanels = readUByte(f) # CUInt8
        self.forcedLodLevel = readUByte(f) # CUInt8
        self.shadowBias = readUByte(f) # CUInt8
        self.renderingPlane = readUByte(f) # CUInt8


        endp = f.tell();
        read = endp - startp;
        if (read < size):
            f.seek(size - read, 1)#unk1.Read(file, size - (uint)read);
        elif (read > size):
            log.error("ERROR READING SBlockDataMeshObject")

class SBlockDataRigidBody:
    def __init__(self, f, size, packedObjectType):
        startp = f.tell();

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


        endp = f.tell();
        read = endp - startp;
        if (read < size):
            f.seek(size - read, 1)#unk1.Read(file, size - (uint)read);
        elif (read > size):
            log.error("ERROR READING SBlockDataMeshObject")

class SBlockDataSpotLight: #make CPointLightComponent
    def __init__(self, f, size, packedObjectType):
        #f = bStream()
        startp = f.tell();
        self.color = CColor().Read(f)  #f.readUInt32() #CUInt32 //TODO: Check why this works an CColor doesn't?
        self.radius = f.readFloat() #CFloat
        self.brightness = f.readFloat() #CFloat
        self.attenuation = f.readFloat() #CFloat
        self.autoHideRange = f.readFloat() #CFloat
        self.shadowFadeDistance = f.readFloat() #CFloat
        self.shadowFadeRange = f.readFloat() #CFloat
        self.shadowFadeBlendFactor = f.readFloat() #CFloat
        #self.lightFlickering = CVector3D(f, 0) #SVector3D
        #self.shadowCastingMode = f.readUInt8() #CUInt8
        #self.dynamicShadowsFaceMask = f.readUInt8() #CUInt8
        #self.envColorGroup = f.readUInt8() #CUInt8
        #self.padding = f.readUInt8() #CUInt8
        # self.lightUsageMask = f.readUInt32() #CUInt32
        # self.innerAngle = f.readFloat() #CFloat
        # self.outerAngle = f.readFloat() #CFloat
        # self.softness = f.readFloat() #CFloat
        # self.projectionTextureAngle = f.readFloat() #CFloat
        # self.projectionTexureUBias = f.readFloat() #CFloat
        # self.projectionTexureVBias = f.readFloat() #CFloat
        # self.projectionTexture = f.readUInt16() #CUInt16
        # self.padding2 = f.readUInt16() #CUInt16
        f.seek(6,1)
        endp = f.tell();
        read = endp - startp;
        if (read < size):
            f.seek(size - read, 1)#unk1.Read(file, size - (uint)read);
        elif (read > size):
            log.error("ERROR READING SBlockDataSpotLight")

class SBlockData:
    def __init__(self, f, size, packedObjectType):
        self.rotationMatrix = CMatrix3x3(f)
        self.position = CVector3D(f, 0) #CVector3D
        self.streamingRadius = readUShort(f) #CUInt16
        self.flags = readUShort(f) #CUInt16
        self.occlusionSystemID = readU32(f) #CUInt32
        self.packedObjectType = packedObjectType

        if packedObjectType == Enums.BlockDataObjectType.Mesh:
            self.packedObject = SBlockDataMeshObject(f, size - 56, packedObjectType)
            self.resourceIndex = self.packedObject.meshIndex
        elif packedObjectType == Enums.BlockDataObjectType.RigidBody: # actuall rigid bodies?
            self.packedObject = SBlockDataRigidBody(f, size - 56, packedObjectType)
            self.resourceIndex = self.packedObject.meshIndex
        elif packedObjectType == Enums.BlockDataObjectType.Collision: # actuall rigid bodies?
            self.packedObject = SBlockDataCollisionObject(f, size - 56, packedObjectType)
            self.resourceIndex = self.packedObject.meshIndex
        elif packedObjectType == Enums.BlockDataObjectType.Dimmer:
            self.packedObject = SBlockDataDimmer(f, size - 56, packedObjectType)
            #self.resourceIndex = self.packedObject.meshIndex
        # elif packedObjectType == Enums.BlockDataObjectType.Invalid:
        #     self.packedObject = SBlockDataMeshObject(f, size - 56, packedObjectType)
        #     self.resourceIndex = self.packedObject.meshIndex
        #elif packedObjectType == Enums.BlockDataObjectType.SpotLight:
            #self.resourceIndex = readUShort(f)
            #self.packedObject = SBlockDataSpotLight(f, size - 58, packedObjectType)
            #self.resourceIndex = self.packedObject.meshIndex
        else:
            self.resourceIndex = readUShort(f)
            f.seek(size - 58, 1)
            #self.tail = #CBytes
            #f.seek(size - 56, 1)

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
    def __init__(self, f, CR2WFILE, parent):
        self.ChunkHandle = False
        self.Reference = None
        self.val = readInt32(f) # Int32
        self.DepotPath = None
        self.ClassName = None
        self.Flags = None
        val = self.val

        if (val >= 0):
            self.ChunkHandle = True;
        if (self.ChunkHandle):
            if (val == 0):
                self.Reference = None;
            else:
                self.Reference = self.val - 1 #CR2WFILE
                #Reference = cr2w.chunks[val - 1];
        else:
            try:
                self.DepotPath = CR2WFILE.CR2WImport[-val - 1].path;

                filetype = CR2WFILE.CR2WImport[-val - 1].className;
                self.ClassName = CR2WFILE.CNAMES[filetype].name.value;

                self.Flags = CR2WFILE.CR2WImport[-val - 1].flags;
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
    def __init__(self, f):
        self.X = 0.0
        self.Y = 0.0
        self.Z = 0.0
        self.Pitch = 0.0
        self.Yaw = 0.0
        self.Roll = 0.0
        self.Scale_x = 1.0
        self.Scale_y = 1.0
        self.Scale_z = 1.0

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
            return self.Index.ToString()
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

    def __init__(self, f, CR2WFILE, parent, no_name = False, custom_propstart=False):
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

        if self.theName == "bones":
            log.debug("bones hit")
        # if self.theName == "data":
        #     log.debug("data hit")
        # if self.theName == "parentIndices":
        #     log.debug("parentIndices")
        # if self.theName == "mimicPoses":
        #     log.debug("mimicPoses")
        # if self.theName == "animations":
        #     log.debug("animations")
        # if self.theName == "cookedMipStackHeight":
        #     log.debug("cookedMipStackHeight")
        # if self.theName == "streamingDataBuffer":
        #     log.debug("streamingDataBuffer")
        # if self.theName == "tags":
        #     log.debug("tags")
        # if self.theName == "containers":
        #     log.debug("containers")
        # if self.theName == "apexMaterialNames":
        #     log.debug("apexMaterialNames")
        # if self.theName == "Diffuse":
        #     log.debug("Diffuse")
        if self.theName == "appearances":
            log.debug("appearances")
        if self.theName == "ParentSlotName":
            log.debug("ParentSlotName")
        if self.theName == "animations":
            log.debug("animations")

        # if "aterial" in Type.type:
        #     log.debug("aterial")
        # log.debug(Type.type, f.tell())
        # if self.classEnd == 13009453:
        #     log.debug("classend prob")
        
        if "ESkeletalAnimationType" in Type.type:
            log.debug("ESkeletalAnimationType")
        
            
        #detect array type:
        
        More = []
        #? to make the animation list load faster save reading the bones until needed
        #if ("array:129,0,SAnimationBufferBitwiseCompressedBoneTrack" == Type.type):
        if (",SAnimationBufferBitwiseCompressedBoneTrack" in Type.type):
            self.Count = readU32(f)
            for _ in range(0, self.Count):
                More.append(SAnimationBufferBitwiseCompressedBoneTrack(CR2WFILE).Read(f, 0, self.classEnd))
            self.More = More
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
        elif (theType == "CGUID"):
            f.seek(dataEnd)
        elif ("array" in theType or "static:" in theType or "curveData" in theType or "]" in theType):
            if (":" in theType):
                delim = theType.find(':')
                arrayDataType = theType[0:delim] #SubStr( theType, 0, delim);
            else:
                delim = theType.find(']')
                arrayDataType = theType[delim+1:len(theType)]#SubStr( theType, delim+1, len(theType) - delim);
            #cake = len(theType) - delim 0 delim+1;
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
            if (doesExist(Type.type, "]")):
                f.seek(8,1)#FSkip(8);
                count = (int)((strartofthis + Type.size + 4 - f.tell()) / 4);
            #iTemp, jTemp, zTemp, kTemp, val = i, j, z, k, 0; # local uint64 hidden
            self.Handles = []
            startofHandles = f.tell()
            # struct {
            #     HANDLE Handle[count];
            # } Handles;
            for _ in range(0, count):
                self.Handles.append(HANDLE(f,CR2WFILE, self))
            f.seek(startofHandles); # FSeek(startof(Handles));

            # handleIdx = 0; #local uint
            # for j in range(0, count): #for (j=0; j < count; j++) {
            #     val = 0;
            #     if (readU32Check(f,f.tell())-1 > -1 and exists(CR2WFILE, "CR2WExport[readU32Check(f,f.tell())-1].name)")):
            #         val = readU32Check(f,f.tell())-1;
            #     elif (readUShortCheck(f, f.tell())-1 > -1 and exists(CR2WFILE, "CR2WExport[readUShortCheck(f, f.tell())-1].name")):
            #         val = readUShortCheck(f, f.tell())-1;
            #     if (readU32Check(f,f.tell()) != 0 and sortByHandle and CR2WFILE.exports[val] != -1):
            #         Handle = HANDLE(f,CR2WFILE, self);
            #         f.seek(-4,1)#FSkip(-4);
            #         if (Handle[handleIdx].Index > 0 and Handle[handleIdx].Index < CR2WFILE.CR2WTable[4].itemCount):
            #             iTemp = i, jTemp = j, zTemp = z, kTemp = k, i = Handle[handleIdx].Index - 1;
            #             Class = CLASS(f,CR2WFILE, self) #struct CLASS Class <size=SizeCLASS>;
            #             CR2WFILE.exports2[Handle[handleIdx].Index - 1] = 0;
            #             i = iTemp, j = jTemp, k = kTemp, z = zTemp;
            #         handleIdx += 1;
            #     else:
            #         Handle = HANDLE(f,CR2WFILE, self);
            #     f.seek(Handle[j].startof + 4)#FSeek(startof(Handle[j]) + 4);
        elif (Type.type == "array:String" or Type.type == "array:2,0,String"):
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
        elif("ptr:" in Type.type ):
            if (count == 1 and "array" not in Type.type):
                self.Value = readU32(f)
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
            self.Index = STRINGINDEX(f,CR2WFILE, self);
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
        while (f.tell() < parent.classEnd and f.tell() < dataEnd and f.tell() < FileSize(f)-4 and readU32Check(f, f.tell()) != 1462915651):
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
                        return;
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
                            break;
                else:
                    f.seek(1,1);



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
        self.position = CQuaternion(f);
        self.rotation = CQuaternion(f);
        self.scale = CQuaternion(f);

class CCompressedBuffer:
    def __init__(self, f, CR2WFILE, parent, Name = "rigData"):
        self.Name = Name
        self.parent = parent
        self.CR2WFILE = CR2WFILE
        self.rigData = []
    def Read(self, f, size, count):
        m_count = count
        cake = f.tell()
        f.seek(2,1) # TODO CHECK why need this, count??
        for _ in range(0, m_count):
            self.rigData.append(SSkeletonRigData(f))

class CGUID(object):
    """docstring for CGUID."""
    def __init__(self):
        super(CGUID, self).__init__()
        self.guid = bytearray(16)
    def Read(self, f: bStream):
        self.guid = f.read(16)

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
            return;

        typename = self.CR2WFILE.CNAMES[typeId].name.value;
        varname = self.CR2WFILE.CNAMES[nameId].name.value;
        propstart = PROPSTART_BLANK()
        propstart.size = varsize
        propstart.name = varname
        propstart.type = typename
        self.PROP = PROPERTY(br, self.CR2WFILE, self, False, propstart)

class CBufferUInt32():
    def __init__(self, CR2WFILE, buffer_type):
        self.buffer_type = buffer_type;
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
        print("CAke")
        
        #CBufferVLQInt32<SFoliageInstanceData> TreeCollection { get; set; }

class CVariantSizeNameType():
    def __init__(self, CR2WFILE):
        self.CR2WFILE = CR2WFILE
        self.PROP = False
    def Read(self, f, size):
        varsize = readU32(f)#file.ReadUInt32();
        self.classEnd = varsize
        buffer = f.read(varsize - 4)#file.ReadBytes((int)varsize - 4);
        br = bStream(data = bytearray(buffer))
        nameId = readUShort(br)
        typeId = readUShort(br)

        if (nameId == 0):
            return;

        typename = self.CR2WFILE.CNAMES[typeId].name.value;
        varname = self.CR2WFILE.CNAMES[nameId].name.value;
        propstart = PROPSTART_BLANK()
        propstart.size = varsize
        propstart.name = varname
        propstart.type = typename
        self.PROP = PROPERTY(br, self.CR2WFILE, self, False, propstart)

class CArray():
    def __init__(self, CR2WFILE, array_type):
        self.array_type = array_type
        self.CR2WFILE = CR2WFILE
        self.elements = []
    def Read(self, f, size):
        elementcount = readU32(f)
        for _ in range(0, elementcount):
            element = self.array_type(self.CR2WFILE)
            element.Read(f, 0)
            self.elements.append(element)

class CMaterialInstance():
    def __init__(self, CR2WFILE):
        self.CR2WFILE = CR2WFILE
        self.InstanceParameters = CArray(CR2WFILE, CVariantSizeNameType) #new CBufferUInt32<CVariantSizeTypeName>(cr2w, this, nameof(variables)) { IsSerialized = true };
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



class CLASS:
    def GetVariableByName(self, str):
        for item in self.PROPS:
            if item.theName == str:
                return item
        return None

    def __init__(self, f, CR2WFILE, parent, idx = False):
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
        # if self.name == "CHardAttachment":
        #     log.debug("CHardAttachment")
        # if self.name == "CHardAttachment":
        #     log.debug("CHardAttachment")
        tempClass  = parent.currentClass; # local string
        if currentClass == "CMesh":
            while True:
                prop = PROPERTY(f, CR2WFILE, self)
                if prop.Type == None:
                    break
                self.PROPS.append(prop)
            #ReadAllRedVariables
            #REDBuffers
            self.CMesh = CMesh(CR2WFILE)
            self.CMesh.Read(f, 0)
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
                    print("cake")

            count = ReadVLQInt32(f)
            self.ChildrenInfos = []
            for _ in range(0, count):
                self.ChildrenInfos.append(HANDLE(f, CR2WFILE, self))
                if self.ChildrenInfos[-1].Reference > 100000:
                    cake1 = readInt32(f)
                    cake2 = readInt32(f)
                    cake3 = readInt32(f)
                    print("cake")

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
                print("cake")
            f.seek(self.classEnd)
        elif (CR2WFILE.CR2WExport[idx].dataSize == 5 and readUShortCheck(f.tell()+2) < CR2WFILE.CR2WTable[1].itemCount):
            f.seek(startofthis + 2)#FSeek(startof(this) + 2);
            #STRINGINDEX scnAnimName;
        elif (CR2WFILE.CR2WExport[idx].dataSize > 3):
            dataEnd = f.tell() + CR2WFILE.CR2WExport[idx].dataSize; #local uint64
            idxTotals = 0; #local int
            cake = []
            cakeEnd = []
            while (f.tell() < self.classEnd-1 and f.tell() + 4 < FileSize(f) and readU32Check(f, f.tell()) != 1462915651):
                if (detectedProp(f, CR2WFILE, f.tell()) and f.tell()+4 < self.classEnd):
                    cake.append(f.tell())
                    if self.propCount == 1:
                        cak6547e = "fwa"
                    start_time = time.time()
                    self.PROPS.append(PROPERTY(f, CR2WFILE, self))
                    time_taken = time.time() - start_time
                    log.debug(' Read PROP in %f seconds.', time.time() - start_time)
                    if time_taken > 0.3:
                        log.critical("cake")
                    cakeEnd.append(f.tell())
                    if self.PROPS[-1].dataEnd != f.tell():
                        log.warning(r'dataEnd was not correct '+self.name)
                        f.seek(self.PROPS[-1].dataEnd) # TODO NEEDS MORE TESTING
                    if len(self.PROPS) == 16 and currentClass == "CClipMap":
                        log.critical("cake")
                        
                    # setColor();
                    # PROPERTY Property;
                    # if (exists(Property.Type) && title == "") {
                    #     if (Property.Type.name == "id" || endsWith(Property.Type.name, "Id") || endsWith(Property.Type.type, "Id")) {
                    #         if (exists(Property.Value))
                    #             SPrintf(title, "%s = %Ld", Property.Type.name, Property.Value);
                    #         else if (exists(Property.More[0].Value))
                    #             SPrintf(title, "%s = %Ld", Property.Type.name, Property.More[0].Value);
                    #     } else if (doesExist(Property.Type.type, "Ref") && exists(Property.Path)) {
                    #         title = Property.Path.Path;
                    #     }
                    # }
                    self.propCount+=1
                else:
                    # if self.name == "CHardAttachment":
                    #     f.seek(1,1);
                    #     cake = PROPERTY(f, CR2WFILE, self)
                    #     pdwa = 432
                    if self.name in Entity_Type_List:
                        self.isCreatedFromTemplate = False
                        self.Template = self.GetVariableByName('template')
                        #self.Transform = self.GetVariableByName('transform').EngineTransform
                        if self.Template and self.Template.Handles and self.Template.Handles[0].DepotPath:
                            self.isCreatedFromTemplate = True
                        self.Components = []
                        f.seek(10,1)
                        size = self.classEnd - startofthis
                        endPos = f.tell();
                        bytesleft = size - (endPos - startofthis);
                        log.info(self.name)
                        if (not self.isCreatedFromTemplate):
                            if bytesleft > 0:
                                elementcount = ReadBit6(f)
                                for item in range(0,elementcount):
                                    self.Components.append(readInt32(f))


                        endPos = f.tell();
                        bytesleft = size - (endPos - startofthis);
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

                        endPos = f.tell();
                        bytesleft = size - (endPos - startofthis);
                        self.BufferV2 = False
                        if (self.isCreatedFromTemplate):
                            self.BufferV2 = CBufferUInt32(CR2WFILE, SEntityBufferType2)
                            if (bytesleft > 0):
                                self.BufferV2.Read(f, 0);
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
                        #     cake = f.tell()
                        #     self.entries.append(PROPERTY(f,CR2WFILE,self, True));
                        #     f.seek(-2,1);
                        f.seek(self.classEnd)
                    elif self.name == "CSkeleton":
                        for item in self.PROPS:
                            if item.theName == "bones":
                                bonecount = len(item.More);
                                break;
                        self.rigData = CCompressedBuffer(f, CR2WFILE, self, Name = "rigData")
                        self.rigData.Read(f, bonecount * 48, bonecount);
                        #f.seek(self.classEnd)
                    elif self.name == "CFoliageResource":
                        self.Trees = CBufferVLQInt32(CR2WFILE, SFoliageResourceData)
                        self.Trees.Read(f, 0)
                        self.Grasses = CBufferVLQInt32(CR2WFILE, SFoliageResourceData)
                        self.Grasses.Read(f, 0)
                    elif self.name == "CSectorData":
                        dict = Cr2wResourceManager().Get()
                        f.seek(-1,1);
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
                                nextobj = self.Objects[idx + 1];
                                nextoffset = nextobj.offset; #ulong
                                leng = nextoffset - curoffset;
                            else:
                                leng = self.blocksize - curoffset;
                            self.BlockData.append(SBlockData(f, leng, curobj.type))
                            idx += 1
                        # for _ in range(0, count):
                        #     self.BlockData.append(SBlockData(f))

                        f.seek(self.classEnd)
                    elif self.name == "CGameWorld":
                        f.seek(2,1);
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
                        f.seek(2,1);
                        endpos = f.tell();
                        bytesread = endpos - startofthis;
                        self.ParentGroup = HANDLE(f, CR2WFILE, self)
                        f.seek(self.classEnd)
                    elif self.name == "CMaterialInstance":
                        f.seek(2,1); #TODO what is this skip?
                        MyMaterialInstance = CMaterialInstance(CR2WFILE)
                        MyMaterialInstance.Read(f)
                        self.InstanceParameters = MyMaterialInstance.InstanceParameters
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
    def __init__(self,f):
        self.className = readUShort(f)
        self.classFlags = readUShort(f)
        self.propertyName = readUShort(f)
        self.propertyFlags = readUShort(f)
        self.hash = readU64(f)

class CR2WBuffer:
    def __init__(self,f):
        self.flags = readU32(f)
        self.index = readU32(f)
        self.offset = readU32(f)
        self.diskSize = readU32(f)
        self.memSize = readU32(f)
        self.crc32 = readU32(f)

class CR2WExport:
    def __init__(self,f, CR2WFILE):
        self.className = STRINGINDEX(f, CR2WFILE, self)# STRINGINDEX className;
        self.objectFlags = readUShort(f)# ushort objectFlags;
        self.parentID = readU32(f) # uint parentID
        self.dataSize = readU32(f) # uint dataSize
        self.dataOffset = readU32(f) # uint dataOffset
        self.template = readU32(f) # uint template
        startofcrc32 = f.tell()
        self.crc32 = readU32(f); # uint crc32;
        self.name = CR2WFILE.CNAMES[self.className.Index].name.value # local string name <hidden=true> = CR2WFile[level].NAMES.Name[className.Index].name;
        f.seek(self.dataOffset + CR2WFILE.start) # FSeek(dataOffset + start);
        # struct { FSkip(dataSize); } Data;
        f.seek(self.dataSize, 1)
        f.seek(startofcrc32 + 4)# FSeek(startof(crc32)+4);

    def ReadCR2WExport():
        #string ReadCR2WEXPORT (CR2WEXPORT &input) { return input.name; }
        pass

class CR2WImport:
    def __init__(self,f, CR2WFILE):
        startofdepotPath = f.tell()
        self.depotPath = readU32(f)
        f.seek(self.depotPath + CR2WFILE.CR2WTable[0].offset + CR2WFILE.start);
        self.path = getString(f)#string path <open=suppress>;
        f.seek(startofdepotPath+4);
        self.className = readUShort(f)
        self.flags = readUShort(f)

    def ReadCR2WIMPORT():
        #string ReadCR2WIMPORT (CR2WIMPORT &input) { return input.path; }
        pass
    def WriteCR2WIMPORT():
        #void WriteCR2WIMPORT (CR2WIMPORT &f, string s ) { forceWriteString(startof(f.path), sizeof(f.path), s); }
        pass


# uint64 GenerateHash(CNAME str) {
#     local uint64 fnvhash = 0xCBF29CE484222325;

#     if (sizeof(str) == 1 && str[0] == 0)
#         return 0;

#     for (j = 0; j < sizeof(str)-1; j++) {
#         fnvhash ^= str[j];
#         fnvhash *= 0x00000100000001B3;
#     }
#     return (uint32)(0xFFFFFFFF & fnvhash);
# }

def getCR2WTABLEName(index, version):
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

        if version == 112:
            pass
        else:
            self.crc32 = readU32(f) #uint crc32;

class CR2W_header:
    def __init__(self,f):
        self.magic = readU32(f)
        self.version = readU32(f) # witcher3 = 162

        if (self.version < 115):# witcher2
            self.flags = readU32(f)
            log.error("w2 header error")
        else:
            self.flags = readU32(f)
            self.timestamp = readU64(f)
            self.buildVersion = readU32(f)
            self.fileSize = readU32(f)
            self.bufferSize = readU32(f)
            self.CRC32 = readU32(f)
            self.numChunks = readU32(f)

class CR2W:
    def __init__(self, f, anim_name = None):
        self.fileName = f.name
        #global variables to use
        self.gName = ""
        self.gType = ""
        start = f.tell()
        self.start = start
        self.HEADER = CR2W_header(f)
        table_range = 10
        if self.HEADER.version == 112: table_range = 4
        
        
        if (self.HEADER.version < 115): #witcher2
            nameDataOffset:int = readU32(f)
            nameCount:int = readU32(f)
            objectDataOffset:int = readU32(f)
            objectCount:int = readU32(f)
            linkDataOffset:int = readU32(f)
            linkCount:int = readU32(f)
            dependencyDataOffset:int = 0
            dependencyCount:int = 0

            if (self.HEADER.version >= 46):
                dependencyDataOffset:int = readU32(f)
                dependencyCount:int = readU32(f)
                
            if (nameDataOffset > 0):
                self.STRINGS = []
                f.seek(nameDataOffset + start)
                for _ in range(0, nameCount): #uses enum table count??
                    self.STRINGS.append(STRING(f).String)
            log.debug("cake")

            if (dependencyDataOffset > 0 and dependencyCount >1):
                self.Dependencies = []
                f.seek(dependencyDataOffset + start)
                for _ in range(0, dependencyCount): #uses enum table count??
                    self.Dependencies.append(STRING(f).String)
        else:

            self.CR2WTable = []
            for i in range(0, table_range):
                self.CR2WTable.append(CR2WTABLE(i, f, self.HEADER.version))

            self.maxExport = 0
            if (self.CR2WTable[4].itemCount > self.maxExport):
                self.maxExport = self.CR2WTable[4].itemCount

            #useless?
            if (self.CR2WTable[0].offset > 0):
                self.STRINGS = []
                f.seek(self.CR2WTable[0].offset + start)
                for _ in range(0, self.CR2WTable[1].itemCount): #uses enum table count??
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
                for _ in range(0, self.CR2WTable[2].itemCount): #uses enum table count??
                    self.CR2WImport.append(CR2WImport(f, self))

            if (self.CR2WTable[3].offset > 0) :
                self.CR2W_Property = []
                f.seek(self.CR2WTable[3].offset + start)
                for _ in range(0, self.CR2WTable[3].itemCount): #uses enum table count??
                    self.CR2W_Property.append(CR2WProperty(f))

            if (self.CR2WTable[4].offset > 0) :
                self.CR2WExport = []
                f.seek(self.CR2WTable[4].offset + start)
                for _ in range(0, self.CR2WTable[4].itemCount): #uses enum table count??
                    self.CR2WExport.append(CR2WExport(f, self))

            if (self.CR2WTable[5].offset > 0) :
                self.CR2WBuffer = []
                f.seek(self.CR2WTable[5].offset + start)
                for _ in range(0, self.CR2WTable[5].itemCount): #uses enum table count??
                    self.CR2WBuffer.append(CR2WBuffer(f))



            self.CHUNKS = DATA(f, self, anim_name)

def getCR2W(f, anim_name = None):
    return CR2W(f, anim_name)


