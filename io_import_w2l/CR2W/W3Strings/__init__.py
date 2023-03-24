import sys
import os
import json
import time

from .W3StringManager import W3StringManager

# file = W3StringFile()
# stream = bStream(path = r"")
# file.Read(stream)

def LoadStringsManager():
    try:
        return W3StringManager.Get()
    except Exception as e:
        raise e
