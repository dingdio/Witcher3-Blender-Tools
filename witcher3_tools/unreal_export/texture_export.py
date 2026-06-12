"""Texture conversion for Unreal export bundles.

Witcher textures are deduped by depot path and written to flat files under the
bundle's ``Textures`` directory. Unreal still imports them at the mirrored depot
path from the manifest's ``depot_path`` field.
"""

from __future__ import annotations

import os
import shutil
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
    """Resolves, converts, and dedupes bundle textures keyed by depot path.

    Files are written flat into ``Textures/``. Mirroring the depot tree on disk
    pushed DLC paths past Windows MAX_PATH inside Blender (not
    long-path aware). Unreal placement comes from the manifest's
    ``depot_path``, not the bundle file location.
    """

    def __init__(self, bundle_root: str):
        self.bundle_root = bundle_root
        self.textures_root = os.path.join(bundle_root, "Textures")
        self.entries: dict[str, dict[str, Any]] = {}
        self.warnings: list[str] = []
        self._failed: set[str] = set()
        self._used_stems: dict[str, str] = {}

    def register(self, raw_value: str, param_name: str) -> Optional[dict[str, Any]]:
        """Resolve + convert one texture reference; returns depot refs or None."""
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
        return {"depot": depot_rel, "rough_depot": entry.get("_rough_depot")}

    def manifest_entries(self) -> list[dict[str, Any]]:
        return [
            {key: value for key, value in entry.items() if not key.startswith("_")}
            for entry in self.entries.values()
        ]

    # ---- internals ----

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
        try:
            output_path = convert_texture_for_unreal(source, out_dir, stem)
        except Exception as exc:
            self.warnings.append(f"failed to convert '{source}': {exc}")
            return None

        entry: dict[str, Any] = {
            "depot_path": depot_rel,
            "file": relpath_for_manifest(output_path, self.bundle_root),
            "srgb": texture_srgb_for_param(param_name),
            "compression": texture_compression_for_param(param_name),
        }

        if entry["compression"] == "normalmap":
            rough_depot = f"{depot_rel}_rough"
            rough_path = os.path.join(out_dir, f"{stem}_rough.png")
            if extract_alpha_to_grayscale_png(output_path, rough_path):
                entry["_rough_depot"] = rough_depot
                self.entries[rough_depot] = {
                    "depot_path": rough_depot,
                    "file": relpath_for_manifest(rough_path, self.bundle_root),
                    "srgb": False,
                    "compression": "masks",
                }

        self.entries[depot_rel] = entry
        return entry


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


def convert_texture_for_unreal(source_path: str, textures_dir: str, base_name: str) -> str:
    from ..CR2W.common_blender import win_safe_path

    ext = Path(source_path).suffix.lower()
    os.makedirs(win_safe_path(textures_dir), exist_ok=True)
    output_path = os.path.join(textures_dir, f"{base_name}.png")
    if ext in (".xbm", ".dds"):
        from ..CR2W import texconv_wrapper

        if ext == ".xbm":
            from ..CR2W import texture_converters

            dds_path = texture_converters.convert_xbm_to_dds(source_path, force=False)
        else:
            dds_path = source_path
        # texconv names its output after the dds stem; convert in a scratch
        # folder so same-named textures from different depot dirs can't
        # overwrite each other's finished files.
        scratch_dir = os.path.join(textures_dir, "_convert_tmp")
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


def extract_alpha_to_grayscale_png(source_png: str, output_png: str) -> bool:
    try:
        import bpy
    except Exception:
        return False

    image = bpy.data.images.load(source_png, check_existing=True)
    width, height = image.size
    if width <= 0 or height <= 0 or image.channels < 4:
        return False
    pixels = list(image.pixels)
    alpha_values = pixels[3::4]
    if not alpha_values:
        return False
    if max(alpha_values) - min(alpha_values) < 0.001:
        return False

    rough = bpy.data.images.new(Path(output_png).stem, width=width, height=height, alpha=True, float_buffer=False)
    rough_pixels = []
    for alpha in alpha_values:
        rough_pixels.extend((alpha, alpha, alpha, 1.0))
    rough.pixels.foreach_set(rough_pixels)
    rough.update()
    rough.filepath_raw = output_png
    rough.file_format = "PNG"
    rough.save()
    bpy.data.images.remove(rough)
    return True
