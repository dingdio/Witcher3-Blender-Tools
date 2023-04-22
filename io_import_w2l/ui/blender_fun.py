
import struct
from mathutils import Euler
from math import radians
from io_import_w2l.CR2W.CR2W_types import getCR2W
from io_import_w2l.CR2W import bStream

def reset_transforms(new_obj):
    x, y, z = (radians(0), radians(0), radians(0))
    mat = Euler((x, y, z)).to_matrix().to_4x4()
    new_obj.matrix_world = mat
    new_obj.matrix_local = mat
    new_obj.matrix_basis = mat

    new_obj.location[0] = 0
    new_obj.location[1] = 0
    new_obj.location[2] = 0
    new_obj.scale[0] = 1
    new_obj.scale[1] = 1
    new_obj.scale[2] = 1
    


def convert_xbm_to_dds(fdir):
    f = open(fdir,"rb")
    xbmFile = getCR2W(f)
    
    f.seek(0)
    br:bStream = bStream(data = f.read())
    f.close()
    
    ddsheader = b'\x44\x44\x53\x20\x7C\x00\x00\x00\x07\x10\x0A\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x20\x00\x00\x00\x05\x00\x00\x00\x44\x58\x54\x31\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x08\x10\x40\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'

    for chunk in xbmFile.CHUNKS.CHUNKS:
        if chunk.Type == "CBitmapTexture":
            width = struct.pack('i',chunk.GetVariableByName('width').Value)
            height = struct.pack('i',chunk.GetVariableByName('height').Value)
            
            dxt = chunk.GetVariableByName('compression').Index.String
            
            if  dxt == 'TCM_DXTNoAlpha':
                dxt = b'\x44\x58\x54\x31'#'DXT1'   
            elif dxt == 'TCM_DXTAlpha':
                dxt = b'\x44\x58\x54\x35'#'DXT5' 
            elif dxt == 'TCM_NormalsHigh':
                dxt = b'\x44\x58\x54\x35'#'DXT'   
            elif dxt == 'TCM_Normals':
                dxt = b'\x44\x58\x54\x31'#'DXT5' 
            else:
                pass#print   
            dds_path = fdir.replace('.xbm', '.dds')
            br.seek(chunk.PROPS[-1].dataEnd)
            
            if xbmFile.HEADER.version <= 115:
                br.seek(27, 1)
                new = open(dds_path,'wb')
                new.write(ddsheader)
                new.seek(0xC)
                new.write(height)
                new.seek(0x10)
                new.write(width)
                new.seek(0x54)
                new.write(dxt)
                new.seek(128)
                new.write(br.read(None))
                new.close()
            else:
                is_cooked = False
                for export in xbmFile.CR2WExport:
                    if export.objectFlags == 8192 and export.name == 'CBitmapTexture':
                        is_cooked = True
                        break
                new = open(dds_path,'wb')
                new.write(ddsheader)
                new.seek(0xC)
                new.write(height)
                new.seek(0x10)
                new.write(width)
                new.seek(0x54)
                new.write(dxt)
                new.seek(128)
                
                if is_cooked:
                    new.write(chunk.CBitmapTexture.Residentmip.val)
                else:
                    if len(chunk.CBitmapTexture.Mipdata.bufferData) <= 0:
                        return None
                    bytesource = chunk.CBitmapTexture.Mipdata.bufferData[0].Mip.val
                    for buff in chunk.CBitmapTexture.Mipdata.bufferData:
                        bytesource = bytesource + buff.Mip.val
                    new.write(bytesource)
                
                new.close()
                
                

            break
    return dds_path

