import sys
import os
import json
import time

from .W3StringManager import W3StringManager

# file = W3StringFile()
# stream = bStream(path = r"")
# file.Read(stream)

def LoadStringsManager(do_reload = False):
    try:
        return W3StringManager.Get(do_reload)
    except Exception as e:
        raise e

