"""Persistent Blender-side physics preset storage."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Mapping, Tuple


_STORE_FILENAME = "physics_user_presets.json"
_VALID_KINDS = {"breast", "dyng"}
_SEEDED_FLAG = "_seeded_example_presets"


def normalize_preset_name(name: str) -> str:
    return " ".join(str(name or "").strip().split())


def _extension_package_name(package: str) -> str | None:
    parts = str(package or "").split(".")
    if len(parts) >= 3 and parts[0] == "bl_ext":
        return ".".join(parts[:3])
    return None


def _extension_store_path() -> str | None:
    package = _extension_package_name(__package__)
    if not package:
        return None
    try:
        import bpy

        root = bpy.utils.extension_path_user(package, create=True)
    except Exception:
        return None
    return os.path.join(root, _STORE_FILENAME) if root else None


def _legacy_config_store_path(*, create: bool) -> str | None:
    try:
        import bpy

        root = bpy.utils.user_resource("CONFIG", path="witcher3_tools", create=create)
    except Exception:
        return None
    return os.path.join(root, _STORE_FILENAME) if root else None


def _store_path() -> str:
    path = _extension_store_path()
    if path:
        return path
    path = _legacy_config_store_path(create=True)
    if path:
        return path
    root = os.path.join(os.path.expanduser("~"), ".witcher3_tools")
    os.makedirs(root, exist_ok=True)
    return os.path.join(root, _STORE_FILENAME)


def _package_seed_path() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(__file__)), _STORE_FILENAME)


def _empty_store() -> Dict[str, Any]:
    return {"version": 1, "breast": {}, "dyng": {}, _SEEDED_FLAG: False}


def _normalize_store(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return _empty_store()
    store = _empty_store()
    store[_SEEDED_FLAG] = bool(data.get(_SEEDED_FLAG, False))
    version = data.get("version", 1)
    try:
        store["version"] = int(version)
    except (TypeError, ValueError):
        store["version"] = 1
    for kind in _VALID_KINDS:
        values = data.get(kind, {})
        if isinstance(values, dict):
            store[kind] = values
    return store


def _read_json_store(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return _empty_store()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return _empty_store()
    return _normalize_store(data)


def _has_saved_presets(store: Mapping[str, Any]) -> bool:
    return any(bool(store.get(kind, {})) for kind in _VALID_KINDS)


def _seed_store_if_needed(store: Dict[str, Any], path: str) -> Dict[str, Any]:
    if store.get(_SEEDED_FLAG) or _has_saved_presets(store):
        return store
    seed_path = _package_seed_path()
    if not seed_path or os.path.abspath(seed_path) == os.path.abspath(path):
        return store
    if not os.path.exists(seed_path):
        return store
    seed = _read_json_store(seed_path)
    if not _has_saved_presets(seed):
        return store
    merged = _empty_store()
    for kind in _VALID_KINDS:
        merged[kind] = dict(seed.get(kind, {}))
    merged[_SEEDED_FLAG] = True
    try:
        _write_store(merged)
    except Exception:
        pass
    return merged


def _read_store() -> Dict[str, Any]:
    path = _store_path()
    store = _read_json_store(path)
    if not _has_saved_presets(store):
        legacy_path = _legacy_config_store_path(create=False)
        if legacy_path and os.path.abspath(legacy_path) != os.path.abspath(path):
            legacy_store = _read_json_store(legacy_path)
            if _has_saved_presets(legacy_store):
                legacy_store[_SEEDED_FLAG] = True
                try:
                    _write_store(legacy_store)
                except Exception:
                    pass
                return legacy_store
    return _seed_store_if_needed(store, path)


def _write_store(store: Mapping[str, Any]) -> None:
    path = _store_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(store, handle, indent=2, sort_keys=True)


def _jsonable(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    try:
        return [_jsonable(item) for item in value]
    except Exception:
        return str(value)


def saved_preset_names(kind: str) -> Tuple[str, ...]:
    kind = str(kind or "").lower()
    if kind not in _VALID_KINDS:
        return ()
    store = _read_store()
    return tuple(sorted(str(name) for name in store.get(kind, {}).keys()))


def get_preset(kind: str, name: str) -> Dict[str, Any] | None:
    kind = str(kind or "").lower()
    name = normalize_preset_name(name)
    if kind not in _VALID_KINDS or not name:
        return None
    value = _read_store().get(kind, {}).get(name)
    return dict(value) if isinstance(value, dict) else None


def save_preset(kind: str, name: str, values: Mapping[str, Any]) -> str:
    kind = str(kind or "").lower()
    name = normalize_preset_name(name)
    if kind not in _VALID_KINDS:
        raise ValueError(f"Unsupported physics preset kind: {kind}")
    if not name:
        raise ValueError("Preset name is empty")
    store = _read_store()
    presets = dict(store.get(kind, {}))
    presets[name] = _jsonable(dict(values))
    store[kind] = presets
    _write_store(store)
    return name


def delete_preset(kind: str, name: str) -> bool:
    kind = str(kind or "").lower()
    name = normalize_preset_name(name)
    if kind not in _VALID_KINDS or not name:
        return False
    store = _read_store()
    presets = dict(store.get(kind, {}))
    if name not in presets:
        return False
    del presets[name]
    store[kind] = presets
    _write_store(store)
    return True
