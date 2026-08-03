#import CR2W_file
import os

from .common_blender import repo_file
from . import CR2W_file


def _new_level_dependency_loader():
    cache = {}
    active_paths = set()

    def load_dependency(resolved_path):
        raw_path = str(resolved_path or "").strip()
        if not raw_path:
            return None
        path = os.path.normpath(os.path.abspath(raw_path))
        cache_key = os.path.normcase(path)
        if cache_key in cache:
            return cache[cache_key]
        if cache_key in active_paths:
            return None
        active_paths.add(cache_key)
        try:
            cr2w_file = CR2W_file.read_CR2W(path)
            level = CR2W_file.create_level(
                cr2w_file,
                path,
                dependency_loader=load_dependency,
            )
            cache[cache_key] = level
            return level
        finally:
            active_paths.discard(cache_key)

    return load_dependency


#these function parse the CR2W files and extract only the data needed for import.
def load_w2l(fileName_in = False):
    fileName = repo_file(r"levels\prolog_village\surroundings\architecture.w2l")
    if fileName_in:
        fileName = fileName_in
    CR2WFile = CR2W_file.read_CR2W(fileName)

    level = CR2W_file.create_level(
        CR2WFile,
        fileName,
        dependency_loader=_new_level_dependency_loader(),
    )
    return level

def load_w2w(fileName_in = False, include_groups: bool = True):
    if fileName_in:
        fileName = fileName_in
    CR2WFile = CR2W_file.read_CR2W(fileName)
    world = CR2W_file.create_world(CR2WFile, fileName, include_groups=include_groups)
    #write_yml(world)
    return world

def load_entity(fileName_in = False):
    if fileName_in:
        fileName = fileName_in
    CR2WFile = CR2W_file.read_CR2W(fileName)
    entity = CR2W_file.create_level(CR2WFile, fileName)
    return entity

def load_foliage(fileName_in = False):
    if fileName_in:
        fileName = fileName_in
    CR2WFile = CR2W_file.read_CR2W(fileName)
    foliage = CR2W_file.create_level(CR2WFile, fileName)
    return foliage

import os
def load_material(fileName_in = False):
    if fileName_in:
        fileName = fileName_in
    if not os.path.exists(fileName):
        return []
    CR2WFile = CR2W_file.read_CR2W(fileName)
    #data = CR2W_file.create_level(CR2WFile, fileName)
    data = CR2WFile.CHUNKS.CHUNKS
    return data

if __name__ == "__main__":
    lip = load_w2l()
