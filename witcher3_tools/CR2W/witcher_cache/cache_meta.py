import json
import os
import time
import hashlib
from typing import Callable, Dict, Iterable, List, Tuple


META_VERSION = 1


def get_meta_path(cache_path: str) -> str:
    return cache_path + ".meta.json"


def load_meta(meta_path: str) -> Dict:
    if not os.path.exists(meta_path):
        return {}
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_meta(meta_path: str, meta: Dict) -> None:
    try:
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, separators=(",", ":"), sort_keys=True)
    except Exception:
        pass


def signatures_match(meta_signature: Dict, current_signature: Dict) -> bool:
    if not meta_signature or not current_signature:
        return False
    return meta_signature.get("hash") == current_signature.get("hash")


def iter_files(root_dirs: Iterable[str], file_predicate: Callable[[str], bool]) -> Iterable[str]:
    for root_dir in root_dirs:
        if not root_dir or not os.path.exists(root_dir):
            continue
        for root, dirs, files in os.walk(root_dir):
            dirs.sort()
            files.sort()
            for name in files:
                full_path = os.path.join(root, name)
                if file_predicate(full_path):
                    yield full_path


def compute_signature(paths: Iterable[str]) -> Dict:
    count = 0
    total_size = 0
    latest_mtime = 0
    sha = hashlib.sha1()

    for path in paths:
        try:
            stat = os.stat(path)
        except Exception:
            continue
        count += 1
        total_size += stat.st_size
        mtime = int(stat.st_mtime)
        if mtime > latest_mtime:
            latest_mtime = mtime

        norm_path = os.path.normcase(os.path.abspath(path))
        sha.update(norm_path.encode("utf-8", "ignore"))
        sha.update(str(stat.st_size).encode("ascii"))
        sha.update(str(mtime).encode("ascii"))

    return {
        "count": count,
        "total_size": total_size,
        "latest_mtime": latest_mtime,
        "hash": sha.hexdigest(),
    }


def make_meta(cache_name: str, cache_path: str, signature: Dict, source: Dict) -> Dict:
    return {
        "version": META_VERSION,
        "cache_name": cache_name,
        "cache_path": cache_path,
        "created_at": int(time.time()),
        "signature": signature,
        "source": source,
    }


def natural_sort_key(s: str):
    import re
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s)]


def get_content_patch_dirs(base_path: str) -> List[str]:
    content = os.path.join(base_path, "content")
    if not os.path.exists(content):
        return []
    content_dirs = [
        d for d in os.listdir(content)
        if os.path.isdir(os.path.join(content, d)) and d.startswith("content")
    ]
    patch_dirs = [
        d for d in os.listdir(content)
        if os.path.isdir(os.path.join(content, d)) and d.startswith("patch")
    ]
    content_dirs.sort(key=natural_sort_key)
    patch_dirs.sort(key=natural_sort_key)
    return [os.path.join(content, d) for d in (content_dirs + patch_dirs)]


def get_dlc_dirs(base_path: str, vanilla_only: bool, vanilla_list: List[str]) -> List[str]:
    dlc_root = os.path.join(base_path, "dlc")
    if not os.path.exists(dlc_root):
        return []
    dlc_dirs = [
        os.path.join(dlc_root, d) for d in os.listdir(dlc_root)
        if os.path.isdir(os.path.join(dlc_root, d))
    ]
    dlc_dirs.sort(key=natural_sort_key)
    if not vanilla_only:
        return dlc_dirs

    vanilla_set = {v.lower() for v in vanilla_list}
    return [d for d in dlc_dirs if os.path.basename(d).lower() in vanilla_set]


def get_mod_dirs(mods_root: str) -> List[str]:
    if not mods_root or not os.path.exists(mods_root):
        return []
    dirs = [
        os.path.join(mods_root, d) for d in os.listdir(mods_root)
        if os.path.isdir(os.path.join(mods_root, d))
    ]
    dirs.sort(key=natural_sort_key)
    return dirs


def signature_w3strings(base_path: str, language: str) -> Tuple[Dict, Dict]:
    subdirs = ["content", "dlc", "mod", "DLC", "MOD"]
    roots = []
    for subdir in subdirs:
        folder = os.path.join(base_path, subdir)
        if os.path.isdir(folder):
            roots.append(folder)

    def _predicate(path: str) -> bool:
        return os.path.basename(path).lower() == f"{language}.w3strings".lower()

    signature = compute_signature(iter_files(roots, _predicate))
    source = {
        "type": "w3strings",
        "language": language,
        "base_path": base_path,
        "roots": roots,
    }
    return signature, source


def signature_w3speech(base_path: str, vanilla_dlc_list: List[str]) -> Tuple[Dict, Dict]:
    roots = get_content_patch_dirs(base_path)
    roots.extend(get_dlc_dirs(base_path, vanilla_only=True, vanilla_list=vanilla_dlc_list))

    def _predicate(path: str) -> bool:
        return path.lower().endswith("enpc.w3speech")

    signature = compute_signature(iter_files(roots, _predicate))
    source = {
        "type": "w3speech",
        "base_path": base_path,
        "roots": roots,
    }
    return signature, source


def signature_bundles_base(base_path: str, vanilla_dlc_list: List[str]) -> Tuple[Dict, Dict]:
    roots = get_content_patch_dirs(base_path)
    roots.extend(get_dlc_dirs(base_path, vanilla_only=True, vanilla_list=vanilla_dlc_list))

    def _predicate(path: str) -> bool:
        return path.lower().endswith(".bundle")

    signature = compute_signature(iter_files(roots, _predicate))
    source = {
        "type": "bundle",
        "base_path": base_path,
        "roots": roots,
        "mods": False,
    }
    return signature, source


def signature_bundles_mods(base_path: str, vanilla_dlc_list: List[str]) -> Tuple[Dict, Dict]:
    mods_root = os.path.join(base_path, "mods")

    roots = get_mod_dirs(mods_root)

    # Non-vanilla DLCs are considered modded DLCs
    dlc_dirs = get_dlc_dirs(base_path, vanilla_only=False, vanilla_list=vanilla_dlc_list)
    vanilla_set = {v.lower() for v in vanilla_dlc_list}
    mod_dlc_dirs = [d for d in dlc_dirs if os.path.basename(d).lower() not in vanilla_set]
    roots.extend(mod_dlc_dirs)

    def _predicate(path: str) -> bool:
        return path.lower().endswith(".bundle")

    signature = compute_signature(iter_files(roots, _predicate))
    source = {
        "type": "bundle",
        "base_path": base_path,
        "roots": roots,
        "mods": True,
    }
    return signature, source
