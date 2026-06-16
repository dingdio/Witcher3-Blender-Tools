"""Manifest helpers for Witcher-to-Unreal export bundles.

Schema v2: assets mirror the Witcher depot layout under a single Unreal
content root (e.g. ``characters\\models\\geralt\\body\\model\\t_01_mg__body_hires.w2mesh``
imports as ``/Game/Witcher3/characters/models/geralt/body/model/t_01_mg__body_hires``).
Material instances are emitted per ``.w2mi`` chain level at their depot paths,
parented up to a master material at the ``.w2mg`` graph's depot path.

This module is intentionally Blender-free so it can be tested outside Blender.
"""

from __future__ import annotations

import os
import re
from typing import Any, Iterable

SCHEMA = "witcher_unreal_export.v2"
DEFAULT_CONTENT_ROOTS = {
    "w2": "/Game/Witcher2",
    "w3": "/Game/Witcher3",
}
DEFAULT_CONTENT_ROOT = DEFAULT_CONTENT_ROOTS["w3"]
FALLBACK_MASTER_DEPOT = r"engine\materials\graphs\pbr_std.w2mg"

# ---- overwrite policy ------------------------------------------------------
# Which existing Unreal assets a re-import is allowed to replace, per asset

OVERWRITE_CATEGORIES = (
    "meshes",
    "skeletons",
    "animations",
    "blueprints",
    "material_instances",
    "materials_base",
    "textures",
)

OVERWRITE_PROTECTED_CATEGORIES = ("materials_base", "textures")
OVERWRITE_PRESETS = ("reuse_all", "overwrite_all", "overwrite_except_base")


def default_overwrite() -> dict[str, bool]:
    """Reuse everything (never overwrite) -- the historical default."""
    return {key: False for key in OVERWRITE_CATEGORIES}


def normalize_overwrite(overwrite: Any = None) -> dict[str, bool]:
    result = default_overwrite()
    if isinstance(overwrite, dict):
        for key in OVERWRITE_CATEGORIES:
            if key in overwrite:
                result[key] = bool(overwrite[key])
    return result


def overwrite_preset(preset: str) -> dict[str, bool]:
    name = str(preset or "").strip().lower()
    if name == "overwrite_all":
        return {key: True for key in OVERWRITE_CATEGORIES}
    if name == "overwrite_except_base":
        return {key: key not in OVERWRITE_PROTECTED_CATEGORIES for key in OVERWRITE_CATEGORIES}
    # "reuse_all" / anything unrecognised -> reuse everything.
    return default_overwrite()

# Witcher texture params that should import as sRGB color data.
_SRGB_PARAM_TOKENS = ("diffuse",)
_NORMAL_PARAM_TOKENS = ("normal", "bump")
_MASK_PARAM_TOKENS = ("rough", "mask", "pattern")


def normalize_depot_path(path: str) -> str:
    return str(path or "").replace("/", "\\").strip().strip("\\").lower()


def normalize_source_game(source_game: str | None) -> str:
    value = str(source_game or "").strip().lower().replace(" ", "")
    return "w2" if value in {"w2", "witcher2", "tw2"} else "w3"


def default_content_root(source_game: str | None = "w3") -> str:
    return DEFAULT_CONTENT_ROOTS[normalize_source_game(source_game)]


def normalize_content_root(content_root: str | None, source_game: str | None = "w3") -> str:
    root = str(content_root or default_content_root(source_game)).replace("\\", "/").rstrip("/")
    if not root.startswith("/"):
        root = "/Game/" + root.lstrip("/")
    return root


def is_w2mg_path(path: str) -> bool:
    return normalize_depot_path(path).endswith(".w2mg")


def is_w2mi_path(path: str) -> bool:
    return normalize_depot_path(path).endswith(".w2mi")


def safe_asset_name(name: str, fallback: str = "WitcherAsset") -> str:
    value = str(name or "").strip()
    value = re.sub(r"\.\d{3}$", "", value)
    value = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_")
    if not value:
        value = fallback
    if value[0].isdigit():
        value = "_" + value
    return value


def depot_asset_rel(depot_path: str) -> str:
    """Depot path -> content-root-relative asset path.

    Forward slashes, no file extension, each segment sanitized for Unreal
    package names. ``characters\\models\\x\\body.w2mesh`` -> ``characters/models/x/body``.
    """
    normalized = normalize_depot_path(depot_path)
    if not normalized:
        return ""
    root, ext = os.path.splitext(normalized)
    if ext and not ext[1:].isdigit():
        normalized = root
    segments = [safe_asset_name(seg, "_") for seg in normalized.split("\\") if seg]
    return "/".join(segments)


def depot_asset_dir(depot_path: str) -> str:
    rel = depot_asset_rel(depot_path)
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def depot_asset_name(depot_path: str) -> str:
    rel = depot_asset_rel(depot_path)
    return rel.rsplit("/", 1)[-1] if rel else ""


def relpath_for_manifest(path: str, bundle_root: str) -> str:
    try:
        rel = os.path.relpath(path, bundle_root)
    except Exception:
        rel = path
    return rel.replace("\\", "/")


def texture_srgb_for_param(param_name: str) -> bool:
    lowered = str(param_name or "").lower()
    return any(token in lowered for token in _SRGB_PARAM_TOKENS)


def texture_compression_for_param(param_name: str) -> str:
    lowered = str(param_name or "").lower()
    if any(token in lowered for token in _MASK_PARAM_TOKENS):
        return "masks"
    if any(token in lowered for token in _NORMAL_PARAM_TOKENS):
        return "normal_rgba"
    return "default"


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def parse_float_sequence(value: Any, expected: int = 4, pad: float = 1.0) -> list[float]:
    if isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        parts = [p.strip() for p in re.split(r"[;,]", str(value or "")) if p.strip()]
    numbers = [_coerce_float(p) for p in parts[:expected]]
    while len(numbers) < expected:
        numbers.append(pad)
    return numbers


def witcher_color_to_linear(value: Any) -> list[float]:
    """CR2W Color params are 0-255 per channel."""
    return [channel / 255.0 for channel in parse_float_sequence(value, 4, 255.0)]


# Witcher param type -> manifest param kind handling.
_TEXTURE_TYPES = {"handle:ITexture", "TEX_IMAGE"}
_SCALAR_TYPES = {"Float", "VALUE"}
# CR2W colors are 0-255; Blender RGB socket values are already linear 0-1.
_COLOR_255_TYPES = {"Color"}
_COLOR_LINEAR_TYPES = {"RGB"}
_VECTOR_TYPES = {"Vector", "COMBXYZ"}
_DEFERRED_TYPES = {"handle:CTextureArray", "handle:CCubeTexture", "TEX_ENVIRONMENT"}


def convert_witcher_param(
    param_name: str,
    param_type: str,
    value: Any,
    register_texture,
    label: str = "",
) -> tuple[list[dict[str, Any]], list[str]]:
    """Convert one Witcher material parameter to manifest param entries.

    ``register_texture(raw_value, param_name)`` resolves/queues a texture for
    export and returns ``{"depot": rel}`` or None. Packed alpha channels are
    handled by the generated Unreal master material, not sidecar textures.
    """
    name = str(param_name or "").strip()
    param_type = str(param_type or "").strip()
    prefix = f"{label}: " if label else ""
    if not name:
        return [], [f"{prefix}unnamed material param skipped"]

    if param_type in _TEXTURE_TYPES:
        raw = str(value or "").strip()
        if not raw or raw.upper() == "NULL":
            return [], []
        registered = register_texture(raw, name) if register_texture else None
        if not registered:
            return [], [f"{prefix}texture for '{name}' could not be resolved: '{raw}'"]
        return [{"name": name, "kind": "texture", "depot": registered["depot"]}], []

    if param_type in _SCALAR_TYPES:
        return [{"name": name, "kind": "scalar", "value": _coerce_float(value)}], []

    if param_type in _COLOR_255_TYPES:
        return [{"name": name, "kind": "vector", "value": witcher_color_to_linear(value)}], []

    if param_type in _COLOR_LINEAR_TYPES:
        return [{"name": name, "kind": "vector", "value": parse_float_sequence(value, 4, 1.0)}], []

    if param_type in _VECTOR_TYPES:
        return [{"name": name, "kind": "vector", "value": parse_float_sequence(value, 4, 0.0)}], []

    if param_type in _DEFERRED_TYPES:
        return [], [f"{prefix}{param_type} param '{name}' is deferred"]

    return [], [f"{prefix}param '{name}' has unsupported type '{param_type or '<empty>'}'"]


def build_manifest(
    *,
    asset_name: str,
    bundle_root: str,
    source_game: str | None = "w3",
    content_root: str | None = None,
    overwrite: dict[str, Any] | None = None,
    meshes: Iterable[dict[str, Any]] = (),
    masters: Iterable[dict[str, Any]] = (),
    materials: Iterable[dict[str, Any]] = (),
    textures: Iterable[dict[str, Any]] = (),
    animations: Iterable[dict[str, Any]] = (),
    rig: dict[str, Any] | None = None,
    blueprint: dict[str, Any] | None = None,
    terrain: dict[str, Any] | None = None,
    placements: dict[str, Any] | None = None,
    warnings: Iterable[str] = (),
) -> dict[str, Any]:
    game = normalize_source_game(source_game)
    manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "asset_name": safe_asset_name(asset_name),
        "bundle_root": os.path.normpath(bundle_root),
        "source_game": game,
        "content_root": normalize_content_root(content_root, game),
        "overwrite": normalize_overwrite(overwrite),
        "meshes": list(meshes),
        "masters": list(masters),
        "materials": list(materials),
        "textures": list(textures),
        "animations": list(animations),
        "warnings": list(warnings),
    }
    if rig:
        manifest["rig"] = rig
    if blueprint:
        manifest["blueprint"] = blueprint
    if terrain:
        manifest["terrain"] = terrain
    if placements:
        manifest["placements"] = placements
    return manifest
