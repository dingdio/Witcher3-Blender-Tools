import re


_LOD_SUFFIX_RE = re.compile(r"_lod(\d+)(?:\.\d{3})?$", re.IGNORECASE)


def lod_level_from_name(name: str, default: int = 0) -> int:
    if not name:
        return default
    match = _LOD_SUFFIX_RE.search(name)
    return int(match.group(1)) if match else default


def object_lod_level(obj, default: int = 0) -> int:
    return lod_level_from_name(getattr(obj, "name", ""), default=default)
