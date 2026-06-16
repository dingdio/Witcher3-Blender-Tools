"""Build manifest material entries from Witcher material chains.

For each Blender material slot this walks the ``.w2mi`` baseMaterial chain
(via :mod:`witcher3_tools.w3_material_reader`) and emits one manifest material
instance per chain level at its depot path, plus a master material spec for
the terminal ``.w2mg`` graph. Local (in-mesh) materials become instances in
the mesh's depot folder, mirroring how REDkit stores them.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from .manifest import (
    FALLBACK_MASTER_DEPOT,
    convert_witcher_param,
    depot_asset_rel,
    is_w2mg_path,
    is_w2mi_path,
    safe_asset_name,
)

log = logging.getLogger(__name__)

TextureRegistrar = Callable[[str, str], Optional[dict]]
VOLUME_MASTER_ASSET = "engine/materials/defaults/volume"


def is_volume_master_path(path: str) -> bool:
    return depot_asset_rel(path).lower() == VOLUME_MASTER_ASSET


def _default_chain_reader(material_path: str, version: int) -> dict[str, Any]:
    from ..w3_material_reader import collect_material_chain

    return collect_material_chain(material_path, version=version)


def _default_params_reader(material_bin) -> dict[str, tuple[str, str]]:
    from ..w3_material_reader import read_local_material_params_from_bin

    return read_local_material_params_from_bin(material_bin)


def _read_enable_mask(material_bin) -> bool:
    try:
        prop = material_bin.GetVariableByName("enableMask")
        return bool(getattr(prop, "Value", False))
    except Exception:
        return False


class ChainBuilder:
    """Accumulates depot-mirrored masters/instances across all mesh slots."""

    def __init__(
        self,
        register_texture: TextureRegistrar,
        *,
        chain_reader=None,
        params_reader=None,
        enable_mask_reader=None,
    ):
        self.register_texture = register_texture
        self._chain_reader = chain_reader or _default_chain_reader
        self._params_reader = params_reader or _default_params_reader
        self._enable_mask_reader = enable_mask_reader or _read_enable_mask
        self.masters: dict[str, dict[str, Any]] = {}
        self.materials: dict[str, dict[str, Any]] = {}
        self.material_order: list[str] = []
        self.warnings: list[str] = []
        self._chain_heads: dict[tuple[int, str], dict[str, str]] = {}

    # ---- public API ----

    def add_slot_material(self, mat_info: dict[str, Any], mesh_asset_dir: str) -> str:
        """Register one Blender slot material; returns its manifest material id."""
        props = mat_info.get("witcher_props") or {}
        mat_name = str(mat_info.get("name") or props.get("name") or "Material")
        base = str(props.get("base_custom") or "")
        version = 115 if str(props.get("material_version") or "").lower() == "witcher2" else 999
        is_local = bool(props.get("local", True))

        parent_ref = self._ensure_chain(base, version, label=mat_name)

        if not is_local and is_w2mi_path(base):
            head_id = parent_ref.get("parent_material") or ""
            if head_id:
                return head_id
            # external w2mi could not be read; fall through and emit a local
            # instance so the slot still gets a material assigned.

        instance_name = safe_asset_name(mat_name, "Material")
        asset_path = f"{mesh_asset_dir}/{instance_name}" if mesh_asset_dir else instance_name
        if asset_path in self.materials:
            return asset_path

        params, param_warnings = self._convert_params(
            props.get("input_props") or [], label=mat_name, blender_types=True
        )
        self.warnings.extend(param_warnings)

        entry: dict[str, Any] = {
            "id": asset_path,
            "name": instance_name,
            "asset_path": asset_path,
            "local": True,
            "enable_mask": bool(props.get("enableMask", False)),
            "params": params,
        }
        entry.update(parent_ref)
        if self._parent_ref_is_volume(parent_ref):
            entry["volume"] = True
        self._append_material(entry)
        self._extend_master_params(parent_ref, params)
        return asset_path

    def ordered_materials(self) -> list[dict[str, Any]]:
        return [self.materials[mat_id] for mat_id in self.material_order]

    def ordered_masters(self) -> list[dict[str, Any]]:
        entries = []
        for rel, master in self.masters.items():
            entry = {"depot_path": rel, "params": list(master["params"].values())}
            if master.get("volume"):
                entry["volume"] = True
            entries.append(entry)
        return entries

    # ---- chain handling ----

    def _ensure_chain(self, base_path: str, version: int, label: str = "") -> dict[str, str]:
        """Ensure manifest entries exist for ``base_path`` and everything above it.

        Returns the parent reference for a child of ``base_path``:
        ``{"parent_master": rel}`` or ``{"parent_material": id}``.
        """
        if not base_path:
            self.warnings.append(f"{label}: no base material path; using pbr_std master fallback")
            return {"parent_master": self._ensure_master(FALLBACK_MASTER_DEPOT)}

        cache_key = (version, depot_asset_rel(base_path))
        cached = self._chain_heads.get(cache_key)
        if cached is not None:
            return dict(cached)

        if is_w2mg_path(base_path):
            ref = {"parent_master": self._ensure_master(base_path)}
            self._chain_heads[cache_key] = dict(ref)
            return ref

        chain_info = self._chain_reader(base_path, version)
        for problem in chain_info.get("errors", []) or []:
            self.warnings.append(f"{label}: {problem}")

        chain = list(chain_info.get("chain", []) or [])
        graph_entry = next((e for e in chain if e.get("chunk_type") == "CMaterialGraph"), None)
        if graph_entry is not None:
            master_rel = self._ensure_master(
                str(graph_entry.get("path") or ""), graph_bin=graph_entry.get("_material_bin")
            )
        else:
            master_rel = self._ensure_master(FALLBACK_MASTER_DEPOT)
            self.warnings.append(
                f"{label}: could not resolve a .w2mg for '{base_path}'; using pbr_std master fallback"
            )

        instance_entries = [e for e in chain if e.get("chunk_type") == "CMaterialInstance"]
        parent_ref: dict[str, str] = {"parent_master": master_rel}
        for entry in reversed(instance_entries):
            parent_ref = {"parent_material": self._ensure_instance(entry, parent_ref, label)}

        if not instance_entries and is_w2mi_path(base_path):
            # Unreadable w2mi: child instances parent straight to the master.
            parent_ref = {"parent_master": master_rel}

        self._chain_heads[cache_key] = dict(parent_ref)
        return parent_ref

    def _ensure_instance(self, chain_entry: dict[str, Any], parent_ref: dict[str, str], label: str) -> str:
        depot_path = str(chain_entry.get("path") or "")
        asset_path = depot_asset_rel(depot_path)
        if asset_path in self.materials:
            return asset_path

        material_bin = chain_entry.get("_material_bin")
        raw_params = self._params_reader(material_bin) if material_bin is not None else {}
        params, param_warnings = self._convert_params(
            [
                {"name": name, "type": type_value[0], "value": type_value[1]}
                for name, type_value in raw_params.items()
            ],
            label=depot_path,
        )
        self.warnings.extend(param_warnings)

        entry: dict[str, Any] = {
            "id": asset_path,
            "name": asset_path.rsplit("/", 1)[-1],
            "asset_path": asset_path,
            "depot_path": depot_path,
            "enable_mask": self._enable_mask_reader(material_bin) if material_bin is not None else False,
            "params": params,
        }
        entry.update(parent_ref)
        if self._parent_ref_is_volume(parent_ref):
            entry["volume"] = True
        self._append_material(entry)
        self._extend_master_params(parent_ref, params)
        return asset_path

    def _ensure_master(self, graph_depot_path: str, graph_bin=None) -> str:
        asset_path = depot_asset_rel(graph_depot_path or FALLBACK_MASTER_DEPOT)
        master = self.masters.get(asset_path)
        if master is None:
            master = {
                "depot_path": graph_depot_path,
                "params": {},
                "volume": is_volume_master_path(graph_depot_path or FALLBACK_MASTER_DEPOT),
            }
            self.masters[asset_path] = master
        elif is_volume_master_path(graph_depot_path or FALLBACK_MASTER_DEPOT):
            master["volume"] = True

        if graph_bin is not None and not master.get("_defaults_read"):
            master["_defaults_read"] = True
            raw_defaults = self._params_reader(graph_bin)
            default_entries, default_warnings = self._convert_params(
                [
                    {"name": name, "type": type_value[0], "value": type_value[1]}
                    for name, type_value in raw_defaults.items()
                ],
                label=graph_depot_path,
            )
            self.warnings.extend(default_warnings)
            for param in default_entries:
                existing = master["params"].get(param["name"])
                if existing is None or "value" not in existing and "depot" not in existing:
                    master["params"][param["name"]] = param
        return asset_path

    def _extend_master_params(self, parent_ref: dict[str, str], params: list[dict[str, Any]]) -> None:
        """Make sure the chain's master declares every param its instances set."""
        master_rel = parent_ref.get("parent_master")
        if not master_rel:
            parent_id = parent_ref.get("parent_material")
            parent = self.materials.get(parent_id or "")
            while parent is not None and not master_rel:
                master_rel = parent.get("parent_master")
                parent = self.materials.get(parent.get("parent_material") or "")
        master = self.masters.get(master_rel or "")
        if master is None:
            return
        for param in params:
            master["params"].setdefault(
                param["name"], {"name": param["name"], "kind": param["kind"]}
            )

    def _parent_ref_is_volume(self, parent_ref: dict[str, str]) -> bool:
        master_rel = parent_ref.get("parent_master")
        if master_rel:
            return bool(self.masters.get(master_rel or "", {}).get("volume"))

        parent_id = parent_ref.get("parent_material")
        visited = set()
        while parent_id and parent_id not in visited:
            visited.add(parent_id)
            parent = self.materials.get(parent_id)
            if not parent:
                return False
            if parent.get("volume"):
                return True
            master_rel = parent.get("parent_master")
            if master_rel:
                return bool(self.masters.get(master_rel or "", {}).get("volume"))
            parent_id = parent.get("parent_material")
        return False

    # ---- helpers ----

    def _convert_params(
        self, input_props: list[dict[str, Any]], label: str, blender_types: bool = False
    ) -> tuple[list[dict[str, Any]], list[str]]:
        params: list[dict[str, Any]] = []
        warnings: list[str] = []
        for prop in input_props:
            if not isinstance(prop, dict):
                continue
            entries, prop_warnings = convert_witcher_param(
                prop.get("name"),
                prop.get("type"),
                prop.get("value"),
                self.register_texture,
                label=label,
            )
            params.extend(entries)
            warnings.extend(prop_warnings)
        return params, warnings

    def _append_material(self, entry: dict[str, Any]) -> None:
        self.materials[entry["id"]] = entry
        self.material_order.append(entry["id"])
