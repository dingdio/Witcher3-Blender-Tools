from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from typing import Any

from .bundle import _resolve_content_root_setting, default_export_folder, overwrite_policy_from_settings
from .manifest import build_manifest, depot_asset_rel, relpath_for_manifest, safe_asset_name


def build_unreal_srt_bundle(context, settings, srt_path: str, depot_path: str = "") -> dict[str, Any]:
    started = time.perf_counter()
    abs_srt_path, resolved_depot_path = _resolve_srt_source(context, srt_path, depot_path)
    if not abs_srt_path or not _path_exists(abs_srt_path):
        raise ValueError(f"SRT file not found: {srt_path}")

    asset_name = safe_asset_name(
        getattr(settings, "asset_name", "")
        or os.path.splitext(os.path.basename(resolved_depot_path or abs_srt_path))[0],
        "SpeedTree",
    )
    export_root = str(getattr(settings, "export_folder", "") or default_export_folder())
    bundle_root = os.path.join(export_root, asset_name)
    os.makedirs(_safe_path(bundle_root), exist_ok=True)

    warnings: list[str] = []
    speedtree_entry, texture_stats = build_speedtree_entry(
        context, settings, abs_srt_path, resolved_depot_path, bundle_root, warnings
    )

    source_game = "w3"
    content_root = _resolve_content_root_setting(getattr(settings, "content_root", ""), source_game)

    manifest = build_manifest(
        asset_name=asset_name,
        bundle_root=bundle_root,
        source_game=source_game,
        content_root=content_root,
        overwrite=overwrite_policy_from_settings(settings),
        speedtrees=[speedtree_entry],
        warnings=warnings,
    )

    manifest_path = os.path.join(bundle_root, "witcher_unreal_export.json")
    with open(_safe_path(manifest_path), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)

    return {
        "asset_name": asset_name,
        "bundle_root": bundle_root,
        "manifest_path": manifest_path,
        "manifest": manifest,
        "srt_path": abs_srt_path,
        "depot_path": resolved_depot_path,
        "texture_stats": texture_stats,
        "elapsed_seconds": time.perf_counter() - started,
    }


def build_speedtree_entry(
    context,
    settings,
    abs_srt_path: str,
    resolved_depot_path: str,
    bundle_root: str,
    warnings: list[str],
    *,
    force_import: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Stage one .srt plus its textures into ``bundle_root`` and return its
    ``speedtrees`` manifest entry and texture stats. Shared by the single-srt
    send and the .flyr foliage send, which stages many trees into one bundle."""
    staged_srt_path = _stage_srt_source(abs_srt_path, resolved_depot_path, bundle_root)
    texture_files, texture_stats = _stage_srt_textures(
        context,
        abs_srt_path,
        resolved_depot_path,
        os.path.dirname(staged_srt_path),
        bundle_root,
        warnings,
    )

    asset_path = depot_asset_rel(resolved_depot_path or os.path.basename(abs_srt_path))
    speedtree_entry = {
        "asset_path": asset_path,
        "depot_path": _depot_display_path(resolved_depot_path),
        "file": relpath_for_manifest(staged_srt_path, bundle_root),
        "texture_files": texture_files,
        "missing_textures": texture_stats["missing"],
        "force_import": bool(force_import),
        "import_options": {
            "tree_scale": 100.0,
            "create_materials": True,
            "include_collision": True,
            "fallback_trunk_collision": True,
            "include_vertex_processing": True,
            "include_wind": True,
            "include_smooth_lod": True,
            "lod_screen_sizes": [1.0, 0.04, 0.02, 0.01, 0.005, 0.0025],
        },
    }

    if texture_stats["requested"] and texture_stats["missing"]:
        warnings.append(
            f"SRT textures missing for {os.path.basename(abs_srt_path)}: "
            + ", ".join(texture_stats["missing"][:8])
            + ("..." if len(texture_stats["missing"]) > 8 else "")
        )
    elif not texture_stats["requested"]:
        warnings.append(
            f"No SRT texture references were discovered for {os.path.basename(abs_srt_path)}; "
            "Unreal will import only textures it can resolve from the .srt file."
        )

    return speedtree_entry, texture_stats


def _resolve_srt_source(context, srt_path: str, depot_path: str = "") -> tuple[str, str]:
    raw = str(srt_path or "").strip().strip('"')
    depot = _depot_display_path(depot_path)
    abs_path = ""

    if raw and (os.path.isabs(raw) or os.path.splitdrive(raw)[0]):
        abs_path = raw
    elif raw:
        try:
            from ..CR2W.common_blender import repo_file

            abs_path = repo_file(raw)
        except Exception:
            abs_path = raw
        if not depot:
            depot = _depot_display_path(raw)

    abs_path = _unprefix(abs_path)
    if not depot and abs_path:
        try:
            from ..importers.import_mesh import get_repo_from_abs_path

            depot = _depot_display_path(get_repo_from_abs_path(os.path.normpath(abs_path)) or "")
        except Exception:
            depot = ""

    if not depot and raw and not (os.path.isabs(raw) or os.path.splitdrive(raw)[0]):
        depot = _depot_display_path(raw)
    return abs_path, depot


def _stage_srt_source(abs_srt_path: str, depot_path: str, bundle_root: str) -> str:
    if depot_path:
        rel_parts = [part for part in _depot_display_path(depot_path).split("\\") if part]
        out_path = os.path.join(bundle_root, "SpeedTrees", *rel_parts)
    else:
        out_path = os.path.join(bundle_root, "SpeedTrees", os.path.basename(abs_srt_path))
    _copy_if_stale(abs_srt_path, out_path)
    return out_path


def _stage_srt_textures(context, abs_srt_path: str, depot_path: str, stage_dir: str, bundle_root: str, warnings: list[str]):
    texture_names = _collect_srt_texture_names(abs_srt_path, warnings)
    stats = {
        "requested": len(texture_names),
        "staged": 0,
        "missing": [],
    }
    if not texture_names:
        return [], stats

    loadmods = bool(getattr(getattr(getattr(context, "scene", None), "witcher_file_browser", None), "loadmods", False))
    srt_dir = os.path.dirname(abs_srt_path)
    srt_rel_folder = os.path.dirname(_depot_display_path(depot_path))
    manager = None
    try:
        from ..CR2W.witcher_cache.TextureCache import LoadTextureManager

        manager = LoadTextureManager(loadmods=loadmods)
    except Exception as exc:
        warnings.append(f"Could not load TextureCache for SRT textures: {exc}")

    staged_files: list[str] = []
    seen: set[str] = set()
    for texture_name in texture_names:
        texture_base = os.path.basename(str(texture_name or "").replace("/", os.sep).replace("\\", os.sep))
        texture_key = texture_base.lower()
        if not texture_base or texture_key in seen:
            continue
        seen.add(texture_key)

        source_path = _find_existing_srt_texture(srt_dir, texture_base)
        if not source_path and manager is not None:
            source_path = _extract_srt_texture_from_cache(manager, texture_base, srt_rel_folder, stage_dir)
        if not source_path or not _path_exists(source_path):
            stats["missing"].append(texture_base)
            continue

        staged = _stage_texture_for_speedtree(source_path, texture_base, stage_dir, warnings)
        if not staged:
            stats["missing"].append(texture_base)
            continue
        staged_files.append(relpath_for_manifest(staged, bundle_root))
        stats["staged"] += 1
    return staged_files, stats


def _collect_srt_texture_names(abs_srt_path: str, warnings: list[str]) -> list[str]:
    try:
        from ..ui import ui_file_browser

        json_path = ui_file_browser._srt_json_from_file(abs_srt_path)
        if not json_path:
            warnings.append(
                f"Could not generate SRT JSON sidecar for {os.path.basename(abs_srt_path)}; "
                "texture staging may be incomplete."
            )
            return []
        return list(ui_file_browser._collect_srt_texture_names(json_path))
    except Exception as exc:
        warnings.append(f"Could not inspect SRT texture references: {exc}")
        return []


def _find_existing_srt_texture(srt_dir: str, texture_base: str) -> str:
    candidates = [os.path.join(srt_dir, texture_base)]
    stem, ext = os.path.splitext(texture_base)
    if ext:
        for alt_ext in (".dds", ".tga", ".png"):
            if alt_ext.lower() != ext.lower():
                candidates.append(os.path.join(srt_dir, stem + alt_ext))
    for candidate in candidates:
        if _path_exists(candidate):
            return candidate
    return ""


def _extract_srt_texture_from_cache(manager, texture_base: str, srt_rel_folder: str, stage_dir: str) -> str:
    item = _choose_srt_texture_cache_item(manager, texture_base, srt_rel_folder)
    if not item:
        return ""
    out_path = os.path.join(stage_dir, texture_base)
    os.makedirs(_safe_path(stage_dir), exist_ok=True)
    try:
        written = item.extract_to_file(out_path)
    except Exception:
        return ""

    candidates = [written, out_path, os.path.splitext(out_path)[0] + ".dds"]
    stem = os.path.splitext(out_path)[0]
    candidates.extend(stem + ext for ext in (".tga", ".png"))
    for candidate in candidates:
        if candidate and _path_exists(candidate):
            return candidate
    return ""


def _choose_srt_texture_cache_item(manager, texture_base: str, srt_rel_folder: str):
    try:
        from ..ui import ui_file_browser

        return ui_file_browser._choose_srt_texture_cache_item(manager, texture_base, srt_rel_folder)
    except Exception:
        pass

    texture_base = os.path.basename(texture_base or "").lower()
    texture_stem = os.path.splitext(texture_base)[0]
    best = None
    best_score = -1
    for key, item_list in getattr(manager, "Items", {}).items():
        if not isinstance(key, str):
            continue
        item = item_list[-1] if isinstance(item_list, list) else item_list
        item_name = str(getattr(item, "Name", None) or key or "").replace("/", "\\")
        item_base = os.path.basename(item_name).lower()
        item_stem = os.path.splitext(item_base)[0]
        if item_base != texture_base and item_stem != texture_stem:
            continue
        item_dir = os.path.dirname(item_name).lower().strip("\\")
        score = 20 if item_base == texture_base else 10
        if srt_rel_folder and item_dir == srt_rel_folder.lower().strip("\\"):
            score += 100
        elif srt_rel_folder and item_dir.startswith(srt_rel_folder.lower().strip("\\") + "\\"):
            score += 40
        if item_name.lower().endswith(".xbm"):
            score += 5
        if score > best_score:
            best_score = score
            best = item
    return best


def _stage_texture_for_speedtree(source_path: str, texture_base: str, stage_dir: str, warnings: list[str]) -> str:
    source_path = _unprefix(source_path)
    texture_base = os.path.basename(texture_base or "")
    if not source_path or not texture_base:
        return ""

    requested_ext = os.path.splitext(texture_base)[1].lower()
    source_ext = os.path.splitext(source_path)[1].lower()
    stage_path = os.path.join(stage_dir, texture_base)

    try:
        if requested_ext == ".dds" and source_ext == ".dds":
            staged = _stage_dds_as_png_payload(source_path, stage_path)
            return staged if _path_exists(staged) else ""

        if requested_ext in {".tga", ".png"} and source_ext == ".dds":
            if _copy_is_fresh(source_path, stage_path):
                return stage_path
            from ..CR2W import texconv_wrapper

            converted = (
                texconv_wrapper.convert_dds_to_tga(source_path, output_dir=stage_dir)
                if requested_ext == ".tga"
                else texconv_wrapper.convert_dds_to_png(source_path, output_dir=stage_dir)
            )
            _copy_if_stale(converted, stage_path)
            return stage_path if _path_exists(stage_path) else ""

        if requested_ext == ".dds" and source_ext == ".xbm":
            from ..CR2W import texture_converters

            scratch_dds = os.path.join(stage_dir, os.path.splitext(texture_base)[0] + "_scratch.dds")
            texture_converters.convert_xbm_to_dds(source_path, force=False, out_path=scratch_dds)
            staged = _stage_dds_as_png_payload(scratch_dds, stage_path)
            return staged if _path_exists(staged) else ""

        if requested_ext and source_ext and requested_ext != source_ext:
            stage_path = os.path.join(stage_dir, os.path.basename(source_path))
            warnings.append(
                f"SRT texture '{texture_base}' staged as '{os.path.basename(stage_path)}'; "
                "Unreal SpeedTree import may require matching texture filenames."
            )
        _copy_if_stale(source_path, stage_path)
        return stage_path if _path_exists(stage_path) else ""
    except Exception as exc:
        warnings.append(f"Failed staging SRT texture '{texture_base}': {exc}")
        return ""


def _stage_dds_as_png_payload(source_path: str, stage_path: str) -> str:
    """Write PNG image data using the .srt's original DDS filename.

    Unreal's SpeedTree importer resolves textures by the filenames embedded in
    the .srt, but its DDS material path can crash when UTextureFactory rejects a
    Witcher DDS. UTextureFactory falls back to image-wrapper detection by bytes,
    so PNG payloads under the original names keep native .srt resolution while
    avoiding that crash path.
    """
    if _copy_is_fresh(source_path, stage_path) and not _file_has_magic(stage_path, b"DDS "):
        return stage_path
    _convert_dds_to_png_payload(source_path, stage_path)
    return stage_path


def _convert_dds_to_png_payload(source_path: str, target_path: str) -> None:
    errors: list[str] = []

    try:
        from PIL import Image

        with Image.open(_safe_path(source_path)) as image:
            image.save(_safe_path(target_path), format="PNG")
        return
    except Exception as exc:
        errors.append(f"Pillow: {exc}")

    try:
        from ..CR2W import texconv_wrapper

        converted = texconv_wrapper.convert_dds_to_png(source_path, output_dir=os.path.dirname(target_path))
        _copy_if_stale(converted, target_path)
        return
    except Exception as exc:
        errors.append(f"texconv: {exc}")

    magick = shutil.which("magick")
    if magick:
        try:
            completed = subprocess.run(
                [magick, _safe_path(source_path), "png:" + _safe_path(target_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode == 0 and _path_exists(target_path):
                return
            errors.append((completed.stderr or completed.stdout or "ImageMagick failed").strip())
        except Exception as exc:
            errors.append(f"ImageMagick: {exc}")

    raise RuntimeError("Could not convert DDS for Unreal SpeedTree import: " + "; ".join(errors))


def _copy_if_stale(source_path: str, target_path: str) -> None:
    source_path = _unprefix(source_path)
    target_path = _unprefix(target_path)
    os.makedirs(_safe_path(os.path.dirname(target_path)), exist_ok=True)
    if _copy_is_fresh(source_path, target_path):
        return
    shutil.copy2(_safe_path(source_path), _safe_path(target_path))


def _copy_is_fresh(source_path: str, target_path: str) -> bool:
    if not _path_exists(target_path):
        return False
    try:
        return (
            os.path.getmtime(_safe_path(target_path)) >= os.path.getmtime(_safe_path(source_path))
            and os.path.getsize(_safe_path(target_path)) > 0
        )
    except Exception:
        return False


def _file_has_magic(path: str, magic: bytes) -> bool:
    try:
        with open(_safe_path(path), "rb") as handle:
            return handle.read(len(magic)) == magic
    except Exception:
        return False


def _depot_display_path(path: str) -> str:
    return str(path or "").replace("/", "\\").strip().strip("\\")


def _safe_path(path: str) -> str:
    try:
        from ..CR2W.common_blender import win_safe_path

        return win_safe_path(path)
    except Exception:
        return path


def _unprefix(path: str) -> str:
    try:
        from ..CR2W.common_blender import win_unprefix_path

        return win_unprefix_path(path)
    except Exception:
        return path


def _path_exists(path: str) -> bool:
    try:
        from ..CR2W.common_blender import win_path_exists

        return win_path_exists(path)
    except Exception:
        return os.path.exists(path)
