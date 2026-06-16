from __future__ import annotations

import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

from .manifest import (
    depot_asset_rel,
    relpath_for_manifest,
    safe_asset_name,
    texture_compression_for_param,
    texture_srgb_for_param,
)


class TextureRegistry:
    def __init__(self, bundle_root: str, parallel: bool = False, prefer_dds: bool = False):
        self.bundle_root = bundle_root
        self.textures_root = os.path.join(bundle_root, "Textures")
        self.parallel = bool(parallel)
        self.prefer_dds = bool(prefer_dds)
        self.entries: dict[str, dict[str, Any]] = {}
        self.warnings: list[str] = []
        self._failed: set[str] = set()
        self._used_stems: dict[str, str] = {}
        self._executor: Optional[ThreadPoolExecutor] = None

    def register(self, raw_value: str, param_name: str) -> Optional[dict[str, Any]]:
        raw = str(raw_value or "").strip().strip('"')
        if not raw or raw.upper() == "NULL":
            return None

        depot_rel = self._derive_depot_rel(raw)
        if not depot_rel:
            return None
        if depot_rel in self._failed:
            return None

        entry = self.entries.get(depot_rel)
        if entry is None:
            entry = self._export_texture(raw, depot_rel, param_name)
            if entry is None:
                self._failed.add(depot_rel)
                return None
        return {"depot": depot_rel}

    def manifest_entries(self) -> list[dict[str, Any]]:
        self._finalize_pending()
        return [
            {key: value for key, value in entry.items() if not key.startswith("_")}
            for entry in self.entries.values()
        ]

    def _derive_depot_rel(self, raw: str) -> str:
        if not os.path.isabs(raw):
            return depot_asset_rel(raw)
        repo_rel = _repo_path_from_abs(raw)
        if repo_rel:
            return depot_asset_rel(repo_rel)
        return depot_asset_rel(f"textures\\{safe_asset_name(Path(raw).stem, 'texture')}")

    def _unique_stem(self, depot_rel: str) -> str:
        base = depot_rel.rsplit("/", 1)[-1]
        stem = base
        counter = 2
        while self._used_stems.get(stem, depot_rel) != depot_rel:
            stem = f"{base}_{counter}"
            counter += 1
        self._used_stems[stem] = depot_rel
        return stem

    def _export_texture(self, raw: str, depot_rel: str, param_name: str) -> Optional[dict[str, Any]]:
        source = resolve_texture_path(raw)
        if not source:
            self.warnings.append(f"texture source not found for '{raw}'")
            return None

        out_dir = self.textures_root
        stem = self._unique_stem(depot_rel)
        compression = texture_compression_for_param(param_name)
        srgb = texture_srgb_for_param(param_name)

        if self.prefer_dds:
            output_path = os.path.join(out_dir, _staged_texture_name(source, stem))
            entry = {
                "depot_path": depot_rel,
                "file": relpath_for_manifest(output_path, self.bundle_root),
                "srgb": srgb,
                "compression": compression,
            }
            if self.parallel:
                entry["_pending_future"] = self._get_executor().submit(stage_texture_as_dds, source, out_dir, stem)
                entry["_source"] = source
            else:
                try:
                    stage_texture_as_dds(source, out_dir, stem)
                except Exception as exc:
                    self.warnings.append(f"failed to stage '{source}': {exc}")
                    return None
            self.entries[depot_rel] = entry
            return entry

        if self.parallel and _can_queue_texture_conversion(source):
            output_path = os.path.join(out_dir, f"{stem}.png")
            entry = {
                "depot_path": depot_rel,
                "file": relpath_for_manifest(output_path, self.bundle_root),
                "srgb": srgb,
                "compression": compression,
                "_pending_future": self._get_executor().submit(convert_texture_for_unreal, source, out_dir, stem),
                "_source": source,
            }
            self.entries[depot_rel] = entry
            return entry

        try:
            output_path = convert_texture_for_unreal(source, out_dir, stem)
        except Exception as exc:
            self.warnings.append(f"failed to convert '{source}': {exc}")
            return None

        entry: dict[str, Any] = {
            "depot_path": depot_rel,
            "file": relpath_for_manifest(output_path, self.bundle_root),
            "srgb": srgb,
            "compression": compression,
        }

        self.entries[depot_rel] = entry
        return entry

    def _get_executor(self) -> ThreadPoolExecutor:
        if self._executor is None:
            workers = max(1, min(4, os.cpu_count() or 1))
            self._executor = ThreadPoolExecutor(max_workers=workers)
        return self._executor

    def _finalize_pending(self):
        pending = [
            (depot_rel, entry, entry.get("_pending_future"))
            for depot_rel, entry in list(self.entries.items())
            if entry.get("_pending_future") is not None
        ]
        for depot_rel, entry, future in pending:
            try:
                future.result()
            except Exception as exc:
                source = entry.get("_source") or depot_rel
                self.warnings.append(f"failed to convert '{source}': {exc}")
                self._failed.add(depot_rel)
                self.entries.pop(depot_rel, None)
                continue
            entry.pop("_pending_future", None)
            entry.pop("_source", None)
        if self._executor is not None:
            self._executor.shutdown(wait=True)
            self._executor = None


def _repo_path_from_abs(file_path: str) -> str:
    try:
        from ..importers.import_mesh import get_repo_from_abs_path

        repo_rel = get_repo_from_abs_path(os.path.normpath(file_path))
    except Exception:
        return ""
    if not repo_rel or os.path.isabs(repo_rel) or os.path.splitdrive(repo_rel)[0]:
        return ""
    return repo_rel


def resolve_texture_path(value: str) -> str:
    from ..CR2W.common_blender import repo_file, win_safe_path, win_unprefix_path

    raw = str(value or "").strip().strip('"')
    if not raw:
        return ""

    candidates = []
    if os.path.isabs(raw):
        candidates.append(raw)
    else:
        candidates.append(repo_file(raw))

    root, ext = os.path.splitext(raw)
    for candidate_ext in (".xbm", ".dds", ".png", ".tga"):
        if ext.lower() != candidate_ext:
            alt = root + candidate_ext
            candidates.append(alt if os.path.isabs(alt) else repo_file(alt))

    for candidate in candidates:
        disk_path = win_unprefix_path(os.path.normpath(candidate))
        try:
            if os.path.isfile(win_safe_path(disk_path)):
                return disk_path
        except Exception:
            if os.path.isfile(disk_path):
                return disk_path
    return ""


def _staged_texture_name(source_path: str, base_name: str) -> str:
    ext = Path(source_path).suffix.lower()
    out_ext = ".dds" if ext in (".xbm", ".dds") else (ext or ".dds")
    return f"{base_name}{out_ext}"


def stage_texture_as_dds(source_path: str, textures_dir: str, base_name: str) -> str:
    from ..CR2W.common_blender import win_safe_path

    ext = Path(source_path).suffix.lower()
    os.makedirs(win_safe_path(textures_dir), exist_ok=True)
    output_path = os.path.join(textures_dir, _staged_texture_name(source_path, base_name))
    if _texture_cache_is_fresh(output_path, source_path):
        return output_path
    if ext == ".xbm":
        from ..CR2W import texture_converters

        dds_path = texture_converters.convert_xbm_to_dds(source_path, force=False)
        return _copy_or_replace(dds_path, output_path)
    return _copy_or_replace(source_path, output_path)


def convert_texture_for_unreal(source_path: str, textures_dir: str, base_name: str) -> str:
    from ..CR2W.common_blender import win_safe_path

    ext = Path(source_path).suffix.lower()
    os.makedirs(win_safe_path(textures_dir), exist_ok=True)
    output_path = os.path.join(textures_dir, f"{base_name}.png")
    if _texture_cache_is_fresh(output_path, source_path):
        return output_path
    if ext in (".xbm", ".dds"):
        from ..CR2W import texconv_wrapper

        if ext == ".xbm":
            from ..CR2W import texture_converters

            dds_path = texture_converters.convert_xbm_to_dds(source_path, force=False)
        else:
            dds_path = source_path
        scratch_dir = os.path.join(textures_dir, "_convert_tmp", base_name)
        png_path = texconv_wrapper.convert_dds_to_png(dds_path, output_dir=scratch_dir)
        result = _copy_or_replace(png_path, output_path)
        try:
            os.remove(win_safe_path(png_path))
        except OSError:
            pass
        return result
    if ext == ".png":
        return _copy_or_replace(source_path, output_path)
    return save_blender_image_as_png(source_path, output_path)


def _can_queue_texture_conversion(source_path: str) -> bool:
    return Path(source_path).suffix.lower() in {".xbm", ".dds"}


def _texture_cache_is_fresh(output_path: str, source_path: str) -> bool:
    from ..CR2W.common_blender import win_safe_path

    try:
        out_safe = win_safe_path(output_path)
        if not os.path.isfile(out_safe):
            return False
        src_safe = win_safe_path(source_path)
        if os.path.isfile(src_safe) and os.path.getmtime(out_safe) < os.path.getmtime(src_safe):
            return False
        return os.path.getsize(out_safe) > 0
    except OSError:
        return False


def _copy_or_replace(source_path: str, output_path: str) -> str:
    from ..CR2W.common_blender import win_safe_path

    if os.path.abspath(source_path) == os.path.abspath(output_path):
        return output_path
    os.makedirs(win_safe_path(os.path.dirname(output_path)), exist_ok=True)
    shutil.copy2(win_safe_path(source_path), win_safe_path(output_path))
    return output_path


def save_blender_image_as_png(source_path: str, output_path: str) -> str:
    import bpy

    image = bpy.data.images.load(source_path, check_existing=True)
    image.filepath_raw = output_path
    image.file_format = "PNG"
    image.save()
    return output_path
