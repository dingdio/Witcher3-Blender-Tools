
from ..bStream import *
import numpy as np


class W3StringBlock2(object):
    def __init__(self, stream:bStream, magic: np.uint):
        self.str_id: np.uint = None
        self.str_key_hex: np.uint = None
        self.Read(stream, magic)
    
    def Create(self):
        pass
    def Read(self, stream:bStream, magic: np.uint):
        self.str_key_hex = stream.readUInt32()

        str_id_n = stream.readUInt32()
        self.str_id = (str_id_n ^ magic)
    def Write(self):
        pass
