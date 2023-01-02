import struct
import io
import csv

import os
blobs = [
r"content\content0\bundles\caketown.bundle",
r"content\content0\bundles\blob.bundle",
r"content\content0\bundles\buffers.bundle",
r"content\content0\bundles\movies.bundle",
r"content\content0\bundles\r4gui.bundle",
r"content\content0\bundles\r4items.bundle",
r"content\content0\bundles\startup.bundle",
r"content\content0\bundles\world_world_runtime.bundle",
r"content\content0\bundles\world_world_startup.bundle",
r"content\content0\bundles\xml.bundle",
r"content\content1\bundles\blob.bundle",
r"content\content1\bundles\buffers.bundle",
r"content\content1\bundles\world_kaer_morhen_runtime.bundle",
r"content\content1\bundles\world_kaer_morhen_startup.bundle",
r"content\content10\bundles\blob.bundle",
r"content\content10\bundles\buffers.bundle",
r"content\content10\bundles\world_the_spiral_runtime.bundle",
r"content\content10\bundles\world_the_spiral_startup.bundle",
r"content\content11\bundles\blob.bundle",
r"content\content11\bundles\buffers.bundle",
r"content\content12\bundles\blob.bundle",
r"content\content12\bundles\buffers.bundle",
r"content\content12\bundles\world_prolog_village_winter_runtime.bundle",
r"content\content12\bundles\world_prolog_village_winter_startup.bundle",
r"content\content2\bundles\blob.bundle",
r"content\content2\bundles\buffers.bundle",
r"content\content2\bundles\world_prolog_village_runtime.bundle",
r"content\content2\bundles\world_prolog_village_startup.bundle",
r"content\content3\bundles\blob.bundle",
r"content\content3\bundles\buffers.bundle",
r"content\content3\bundles\world_wyzima_castle_runtime.bundle",
r"content\content3\bundles\world_wyzima_castle_startup.bundle",
r"content\content4\bundles\blob.bundle",
r"content\content4\bundles\buffers0.bundle",
r"content\content4\bundles\buffers1.bundle",
r"content\content4\bundles\movies0.bundle",
r"content\content4\bundles\movies1.bundle",
r"content\content4\bundles\world_novigrad_runtime.bundle",
r"content\content4\bundles\world_novigrad_startup.bundle",
r"content\content5\bundles\blob.bundle",
r"content\content5\bundles\buffers.bundle",
r"content\content5\bundles\world_skellige_runtime.bundle",
r"content\content5\bundles\world_skellige_startup.bundle",
r"content\content6\bundles\blob.bundle",
r"content\content6\bundles\buffers.bundle",
r"content\content7\bundles\blob.bundle",
r"content\content7\bundles\buffers.bundle",
r"content\content7\bundles\world_island_of_mist_runtime.bundle",
r"content\content7\bundles\world_island_of_mist_startup.bundle",
r"content\content8\bundles\blob.bundle",
r"content\content8\bundles\buffers.bundle",
r"content\content9\bundles\blob.bundle",
r"content\content9\bundles\buffers.bundle",
r"dlc\bob\content\bundles\blob.bundle",
r"dlc\bob\content\bundles\buffers.bundle",
r"dlc\bob\content\bundles\world_bob_runtime.bundle",
r"dlc\bob\content\bundles\world_bob_startup.bundle",
r"dlc\dlc1\content\bundles\blob.bundle",
r"dlc\dlc1\content\bundles\buffers.bundle",
r"dlc\dlc10\content\bundles\blob.bundle",
r"dlc\dlc10\content\bundles\buffers.bundle",
r"dlc\dlc11\content\bundles\blob.bundle",
r"dlc\dlc11\content\bundles\buffers.bundle",
r"dlc\dlc12\content\bundles\blob.bundle",
r"dlc\dlc12\content\bundles\buffers.bundle",
r"dlc\dlc13\content\bundles\blob.bundle",
r"dlc\dlc13\content\bundles\buffers.bundle",
r"dlc\dlc14\content\bundles\blob.bundle",
r"dlc\dlc14\content\bundles\buffers.bundle",
r"dlc\dlc15\content\bundles\blob.bundle",
r"dlc\dlc15\content\bundles\buffers.bundle",
r"dlc\dlc16\content\bundles\blob.bundle",
r"dlc\dlc16\content\bundles\buffers.bundle",
r"dlc\dlc17\content\bundles\blob.bundle",
r"dlc\dlc17\content\bundles\buffers.bundle",
r"dlc\dlc18\content\bundles\blob.bundle",
r"dlc\dlc2\content\bundles\blob.bundle",
r"dlc\dlc2\content\bundles\buffers.bundle",
r"dlc\dlc20\content\bundles\blob.bundle",
r"dlc\dlc20\content\bundles\buffers.bundle",
r"dlc\dlc3\content\bundles\blob.bundle",
r"dlc\dlc4\content\bundles\blob.bundle",
r"dlc\dlc4\content\bundles\buffers.bundle",
r"dlc\dlc5\content\bundles\blob.bundle",
r"dlc\dlc5\content\bundles\buffers.bundle",
r"dlc\dlc6\content\bundles\blob.bundle",
r"dlc\dlc6\content\bundles\buffers.bundle",
r"dlc\dlc7\content\bundles\blob.bundle",
r"dlc\dlc7\content\bundles\buffers.bundle",
r"dlc\dlc8\content\bundles\blob.bundle",
r"dlc\dlc9\content\bundles\blob.bundle",
r"dlc\ep1\content\bundles\blob.bundle",
r"dlc\ep1\content\bundles\buffers.bundle",
]

def fnv1a64(x):
    FnvHashPrime = 0x00000100000001B3
    FnvHashInitial = 0xCBF29CE484222325
    y = str(x)
    for letter in y:
        FnvHashInitial ^= ord(letter)
        FnvHashInitial *= FnvHashPrime
        FnvHashInitial &= 0xffffffffffffffff
    return FnvHashInitial

def hash_bundle_paths(filename):
    filenames = {}
    with open(filename, 'rb') as f:
        magic = f.read(8)
        if magic != b'POTATO70':
            raise Exception(filename+" is not potato!")
        f.seek(16)
        files_offset = struct.unpack('<I', f.read(4))[0]
        num_files = files_offset/320
        f.seek(0x20)
        for _ in range(int(num_files)):
            str_data = f.read(256)
            zero_idx = str_data.index(b"\x00")
            str_data = str_data[:zero_idx]
            path = str_data.decode('ascii')
            hashint = fnv1a64(path)
            filenames.update({path: hashint})
            f.seek(64, 1)
    return filenames

def create_pathhashes(gamedir = r"E:\GOG Games\The Witcher 3 Wild Hunt GOTY", outputPath = None):
    with open(outputPath, 'w', newline='') as csvfile:
        fieldnames = ["Path", "HashInt"]
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)

        writer.writeheader()

        for blob in blobs:
            blobpath = os.path.join(gamedir, blob)
            if os.path.exists(blobpath):
                files = hash_bundle_paths(blobpath)
                for path, hashint in files.items():
                    writer.writerow({'Path': path, 'HashInt': int(hashint)})
            else:
                continue

        print('created pathhashes.csv!')
        
gamedir = r"E:\GOG Games\The Witcher 3 Wild Hunt GOTY"
#create_pathhashes(gamedir)