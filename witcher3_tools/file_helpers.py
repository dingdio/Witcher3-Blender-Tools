import os
from pathlib import Path
import time

def noesisComponentsFound():
    return True

def doesExist(str, str2):
    return str2 in str

def Lower(str):
    return str.lower()

def index_exists(arr, i):
    return len(arr) > i

def getFilenameType(str):
    return os.path.splitext(str)[1]

def getFilenameFile(path):
    return Path(path).stem

def getFilenameFile2(path):
    return getFilenameFile(Path(path).stem)

def waitForFileUnlock(file_path):
    while not os.path.exists(file_path):
        time.sleep(1)
    return True

def rm_ns(str):
    if ':' in str:
        return str.split(":")[-1]
    else:
        return str

class my_checkbox():
    def __init__(self):
        self.checked = False
