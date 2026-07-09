"""Witcher 2 shader-graph compatibility for material node groups.

Witcher 2 .w2mg shader graphs declare parameter names that don't match the
canonical input pins on the bundled Witcher2_* node groups: cloth.w2mg feeds
its diffuse through 'diffusemap' while Witcher2_Main exposes 'Diffuse'.

The old EQUIVALENT_PARAMS table (removed in 059cc0b) redirected the *link* to
the canonical pin instead, which broke export: exporters write the socket name
as the game parameter name, so a W2 material came back with 'Diffuse' instead
of 'diffusemap'.

This module does the conversion the other way around: each W2 .w2mg gets a
bespoke copy of its base node group (e.g. Witcher2_characters_shaders_cloth
from Witcher2_Main) whose input pins are renamed to the parameter names the
graph actually declares. Display wiring keeps working (a renamed pin is still
the same socket internally) and exports stay correct (socket name == game
parameter name). Nothing in here runs for Witcher 3 materials.
"""

import hashlib
import logging
import re
from typing import Dict, Iterable, Optional

try:
    import bpy
except ImportError:  # Allow standalone use (tests, CR2W forensics scripts).
    bpy = None

from .reader import normalize_depot_path, read_declared_graph_params

log = logging.getLogger(__name__)

W2_REPO_VERSION = 115

# Canonical node group pin (lower-case) -> parameter names W2 graphs declare
# for it (lower-case, in priority order). Only names listed here ever cause a
# pin rename; W2 graphs also surface junk names from their parameter buffers
# (ERenderingSortGroup, sortGroup, Uint, ...) that must stay inert.
W2_PIN_EQUIVALENTS: Dict[str, tuple] = {
    'diffuse': ('diffusemap', 'diffuse', 'difuse', 'diff', 'diffusearray', 'tex_diffuse'),
    'normal': ('normalmap', 'normal', 'norm', 'normalarray', 'tex_normalmap'),
    # Witcher3_Eye is reused for W2 eye graphs; its normal pin is NormalBase.
    'normalbase': ('normalmap', 'normal'),
    'speculartexture': ('specularmap', 'specular', 'spec', 'tex_specular'),
    'tintmask': ('ambientmap',),
}

_ALIAS_TO_PIN: Dict[str, str] = {}
for _pin_key, _aliases in W2_PIN_EQUIVALENTS.items():
    for _alias in _aliases:
        _ALIAS_TO_PIN.setdefault(_alias, _pin_key)

_SRGB_PIN_KEYS = {'diffuse', 'speculartexture'}

_BLENDER_MAX_NAME_LENGTH = 63
_NAME_SLUG_RE = re.compile(r"[^0-9A-Za-z]+")


def canonical_w2_pin_key(par_name: str) -> str:
    """Lower-case canonical pin key for a W2 parameter/pin name, or ""."""
    low = str(par_name or "").strip().lower()
    if not low:
        return ""
    if low in W2_PIN_EQUIVALENTS:
        return low
    return _ALIAS_TO_PIN.get(low, "")


def is_w2_srgb_texture_param(par_name: str) -> bool:
    """Whether a W2 texture parameter name carries color data (sRGB)."""
    return canonical_w2_pin_key(par_name) in _SRGB_PIN_KEYS


def w2_bespoke_node_group_name(w2mg_path: str) -> str:
    normalized = normalize_depot_path(w2mg_path)
    if not normalized:
        return ""
    stem = normalized[:-5] if normalized.endswith(".w2mg") else normalized
    slug = _NAME_SLUG_RE.sub("_", stem).strip("_")
    if not slug:
        return ""
    name = f"Witcher2_{slug}"
    if len(name) > _BLENDER_MAX_NAME_LENGTH:
        # Blender truncates datablock names at 63 bytes; keep the name unique.
        digest = hashlib.sha1(normalized.encode("utf-8", "replace")).hexdigest()[:8]
        name = f"{name[:_BLENDER_MAX_NAME_LENGTH - 9]}_{digest}"
    return name


def resolve_w2_bespoke_group_name(w2mg_path: str, version: int = W2_REPO_VERSION) -> str:
    """Name of the bespoke group for a W2 shader graph, or "" when not applicable.

    Applicable means the path is a .w2mg whose declared parameters can be read.
    File reads are cached by the material reader, and nothing is created in
    bpy.data, so this is safe to call from UI draw code.
    """
    normalized = normalize_depot_path(w2mg_path)
    if not normalized.endswith(".w2mg"):
        return ""
    declared = read_declared_graph_params(normalized, version=version)
    if not declared:
        return ""
    return w2_bespoke_node_group_name(normalized)


def _iter_input_socket_items(node_tree):
    if bpy is not None and bpy.app.version >= (4, 0, 0):
        for item in node_tree.interface.items_tree:
            if getattr(item, "item_type", "") == 'SOCKET' and getattr(item, "in_out", "") == 'INPUT':
                yield item
    else:
        yield from node_tree.inputs


def plan_w2_socket_renames(
        declared_params: Iterable[str],
        socket_names: Iterable[str],
        ) -> Dict[str, str]:
    """Map current pin names -> the names the graph declares (exact case).

    Renames are planned, not applied, so the policy stays testable without bpy.
    """
    declared_by_lower: Dict[str, str] = {}
    for name in sorted(str(n) for n in declared_params or () if n):
        declared_by_lower.setdefault(name.lower(), name)

    socket_names = [str(name) for name in socket_names]
    socket_by_lower: Dict[str, str] = {}
    for name in socket_names:
        socket_by_lower.setdefault(name.lower(), name)

    taken = set(socket_names)
    renames: Dict[str, str] = {}

    def claim(old_name: str, new_name: str) -> None:
        renames[old_name] = new_name
        taken.discard(old_name)
        taken.add(new_name)

    # Pass 1: same parameter, different case -> adopt the graph's exact casing.
    for low, declared_name in declared_by_lower.items():
        socket_name = socket_by_lower.get(low)
        if socket_name and socket_name != declared_name and declared_name not in taken:
            claim(socket_name, declared_name)

    # Pass 2: canonical pins -> the equivalent name the graph declares.
    for pin_key, aliases in W2_PIN_EQUIVALENTS.items():
        socket_name = socket_by_lower.get(pin_key)
        if not socket_name or socket_name in renames:
            continue
        if declared_by_lower.get(socket_name.lower()) == socket_name:
            # The graph declares this pin's own name; leave it alone.
            continue
        for alias in aliases:
            declared_name = declared_by_lower.get(alias)
            if declared_name is None:
                continue
            if declared_name in taken:
                continue
            claim(socket_name, declared_name)
            break

    return renames


def _apply_socket_renames(node_tree, renames: Dict[str, str]) -> Dict[str, str]:
    if not renames:
        return {}
    applied: Dict[str, str] = {}
    for item in list(_iter_input_socket_items(node_tree)):
        old_name = str(item.name)
        new_name = renames.get(old_name)
        if not new_name:
            continue
        try:
            item.name = new_name
        except Exception:
            log.warning(
                "Could not rename pin '%s' to '%s' on node group '%s'",
                old_name, new_name, node_tree.name, exc_info=True,
            )
            continue
        if str(item.name) != new_name:
            # Blender adjusted the name; an inexact pin name would export the
            # wrong parameter, so keep the original instead.
            try:
                item.name = old_name
            except Exception:
                pass
            continue
        applied[old_name] = new_name
    return applied


def get_or_create_w2_bespoke_node_group(
        base_node_tree,
        w2mg_path: str,
        version: int = W2_REPO_VERSION,
        ) -> Optional[object]:
    """Return the per-.w2mg copy of a base node group, creating it on demand.

    Returns None when the path is not a readable .w2mg shader graph; callers
    fall back to the shared base group. The base group is never modified.
    """
    if bpy is None or base_node_tree is None:
        return None
    normalized = normalize_depot_path(w2mg_path)
    if not normalized.endswith(".w2mg"):
        return None
    declared = read_declared_graph_params(normalized, version=version)
    if not declared:
        return None
    bespoke_name = w2_bespoke_node_group_name(normalized)
    if not bespoke_name:
        return None

    existing = bpy.data.node_groups.get(bespoke_name)
    if existing is not None:
        return existing

    bespoke = base_node_tree.copy()
    bespoke.name = bespoke_name
    bespoke.use_fake_user = False
    socket_names = [str(item.name) for item in _iter_input_socket_items(bespoke)]
    applied = _apply_socket_renames(bespoke, plan_w2_socket_renames(declared, socket_names))
    bespoke["witcher_w2_base_group"] = str(base_node_tree.name)
    bespoke["witcher_w2mg_path"] = normalized
    if applied:
        log.info(
            "Created W2 node group '%s' from '%s' (pins: %s)",
            bespoke.name,
            base_node_tree.name,
            ", ".join(f"{old}->{new}" for old, new in sorted(applied.items())),
        )
    else:
        log.debug(
            "Created W2 node group '%s' from '%s' (no pin renames needed)",
            bespoke.name,
            base_node_tree.name,
        )
    return bespoke
