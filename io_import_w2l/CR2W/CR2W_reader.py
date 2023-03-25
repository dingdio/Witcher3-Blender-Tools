#import CR2W_file
from .common_blender import repo_file
from . import CR2W_file


#these function parse the CR2W files and extract only the data needed for import.
def load_w2l(fileName_in = False):
    fileName = repo_file("levels\prolog_village\surroundings\architecture.w2l")
    if fileName_in:
        fileName = fileName_in
    CR2WFile = CR2W_file.read_CR2W(fileName)
    level = CR2W_file.create_level(CR2WFile, fileName)
    return level

def load_w2w(fileName_in = False):
    if fileName_in:
        fileName = fileName_in
    CR2WFile = CR2W_file.read_CR2W(fileName)
    world = CR2W_file.create_world(CR2WFile)
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