from .CDLCDefinition import CDLCDefinition, DLCDefinition
from .DLCManager import DLCManager


def LoadDLCManager(source_roots=None, enabled_by_key=None, reset_cache=False):
    return DLCManager.Get(source_roots=source_roots, enabled_by_key=enabled_by_key, reset_cache=reset_cache)
