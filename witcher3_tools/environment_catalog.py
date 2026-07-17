"""Catalog environment definitions available to the preview selector."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PureWindowsPath
from typing import Iterable


ENVIRONMENT_DEFINITIONS_DEPOT_ROOT = PureWindowsPath("environment", "definitions")
ENVIRONMENT_OFF_IDENTIFIER = "ENV_OFF"


@dataclass(frozen=True)
class EnvironmentDefinitionItem:
    identifier: str
    label: str
    depot_path: str
    absolute_path: str


def environment_identifier(depot_path: str) -> str:
    key = str(depot_path or "").replace("/", "\\").casefold()
    return "ENV_" + hashlib.sha1(key.encode("utf-8")).hexdigest()


def scan_environment_definitions(roots: Iterable[str]) -> tuple[EnvironmentDefinitionItem, ...]:
    """Return all native-preview environments from the configured depot roots.

    Scan ``environment\\definitions`` recursively for ``.env`` files. Roots are
    ordered by depot priority, so the first copy of a duplicate path wins.
    """

    found: dict[str, EnvironmentDefinitionItem] = {}
    for root_value in roots or ():
        root_text = str(root_value or "").strip()
        if not root_text:
            continue
        definition_root = Path(root_text).joinpath(*ENVIRONMENT_DEFINITIONS_DEPOT_ROOT.parts)
        try:
            if not definition_root.is_dir():
                continue
        except OSError:
            continue

        for directory, dirnames, filenames in os.walk(definition_root):
            dirnames.sort(key=str.casefold)
            for filename in sorted(filenames, key=str.casefold):
                if Path(filename).suffix.casefold() != ".env":
                    continue
                absolute_path = Path(directory, filename)
                try:
                    relative_path = absolute_path.relative_to(definition_root)
                except ValueError:
                    continue
                label = str(PureWindowsPath(*relative_path.parts))
                depot_path = str(ENVIRONMENT_DEFINITIONS_DEPOT_ROOT / PureWindowsPath(*relative_path.parts))
                depot_key = depot_path.casefold()
                if depot_key in found:
                    continue
                found[depot_key] = EnvironmentDefinitionItem(
                    identifier=environment_identifier(depot_path),
                    label=label,
                    depot_path=depot_path,
                    absolute_path=str(absolute_path),
                )

    return tuple(sorted(found.values(), key=lambda item: (item.label.casefold(), item.label)))


__all__ = (
    "ENVIRONMENT_DEFINITIONS_DEPOT_ROOT",
    "ENVIRONMENT_OFF_IDENTIFIER",
    "EnvironmentDefinitionItem",
    "environment_identifier",
    "scan_environment_definitions",
)
