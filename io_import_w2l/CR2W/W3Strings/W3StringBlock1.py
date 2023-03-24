
from ..bStream import *
import numpy as np


class W3StringBlock1(object):
    def __init__(self, stream:bStream, magic: np.uint):
        self.offset: np.uint = 0
        self.str: str = '' #public string
        self.str_id: np.uint = 0
        self.str_id_hashed: np.uint = 0
        self.strlen: np.uint = 0
        if stream and magic:
            self._construct(stream, magic)
    def _construct(self, stream:bStream, magic: np.uint):
        self.Read(stream, magic)
    
    def Create(self):
        pass
    def Read(self, stream:bStream, magic: np.uint):
        self.str_id_hashed = stream.readUInt32()
        self.str_id = (self.str_id_hashed ^ magic)
        self.offset = stream.readUInt32()
        self.strlen = stream.readUInt32()
    def Write(self):
        pass

    @classmethod
    def from_json(cls, data):
        t_class = cls(None, None)
        for var in data.items():
            #if hasattr(t_class, var[0]):
            setattr(t_class, var[0], var[1])
        return t_class