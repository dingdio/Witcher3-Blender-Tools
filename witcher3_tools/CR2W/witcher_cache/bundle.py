import struct
import csv
import os

from ..common_blender import get_game_path
from . import cache_meta
import logging
log = logging.getLogger(__name__)

PATHHASH_CACHE_VERSION = 1

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
        preamble = f.read(32)
        if preamble[:8] != b'POTATO70':
            raise Exception(filename+" is not potato!")
        files_offset = struct.unpack_from('<I', preamble, 16)[0]
        header_version = struct.unpack_from('<H', preamble, 20)[0]
        entry_size = {3: 320, 5: 304}.get(header_version)
        if not entry_size or files_offset % entry_size:
            raise ValueError(f"Unsupported or malformed bundle header version {header_version}: {filename}")
        f.seek(0x20)
        for _ in range(files_offset // entry_size):
            str_data = f.read(256)
            str_data = str_data.split(b"\x00", 1)[0]
            path = str_data.decode('ascii')
            hashint = fnv1a64(path)
            filenames.update({path: hashint})
            f.seek(entry_size - 256, 1)
    return filenames


def _iter_bundle_files(gamedir):
    roots = (os.path.join(gamedir, name) for name in ("content", "dlc")) if gamedir else ()
    return cache_meta.iter_files(roots, lambda path: path.lower().endswith(".bundle"))


def build_pathhashes_source_signature(gamedir=None):
    gamedir = gamedir or get_game_path() or ""
    gamedir = os.path.normpath(gamedir) if gamedir else ""
    signature = cache_meta.compute_signature(_iter_bundle_files(gamedir))
    signature["hash"] = f"{PATHHASH_CACHE_VERSION}:{signature.get('hash', '')}"
    return signature, {
        "type": "pathhashes",
        "base_path": gamedir,
        "serialization_version": PATHHASH_CACHE_VERSION,
    }


def ensure_pathhashes(gamedir=None, outputPath=None):
    if not outputPath:
        raise ValueError("outputPath is required")
    signature, _source = build_pathhashes_source_signature(gamedir)
    if os.path.exists(outputPath):
        meta = cache_meta.load_meta(cache_meta.get_meta_path(outputPath))
        if cache_meta.signatures_match(meta.get("signature", {}), signature):
            return False
        # Keep the last good cache while the game path is unavailable.
        if not signature.get("count"):
            return False
    create_pathhashes(gamedir=gamedir, outputPath=outputPath)
    return True

def create_pathhashes(gamedir=None, outputPath=None):
    if not outputPath:
        raise ValueError("outputPath is required")
    if gamedir is None:
        gamedir = get_game_path()
    # Replace the CSV atomically for concurrent readers.
    tmp_path = outputPath + ".tmp"
    with open(tmp_path, 'w', newline='') as csvfile:
        fieldnames = ["Path", "HashInt"]
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)

        writer.writeheader()

        for bundle_path in _iter_bundle_files(gamedir):
            for path, hashint in hash_bundle_paths(bundle_path).items():
                writer.writerow({'Path': path, 'HashInt': int(hashint)})

    os.replace(tmp_path, outputPath)
    signature, source = build_pathhashes_source_signature(gamedir)
    meta = cache_meta.make_meta(os.path.basename(outputPath), outputPath, signature, source)
    cache_meta.save_meta(cache_meta.get_meta_path(outputPath), meta)
    log.info("created pathhashes.csv!")
        
#create_pathhashes(gamedir)
