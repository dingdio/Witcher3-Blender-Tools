from typing import List, Tuple, Dict
import numpy as np
import struct

import bStream

# with open(filename,"rb") as f:
#     theFile = getCR2W(f)
#     f.close()
# # f = bStream(path = filename)

class Cache(object):
    def __init__(self):
        super(Cache, self).__init__()
        TextureIdString: bytearray = struct.pack('b', bytes('H'))
cache = Cache()