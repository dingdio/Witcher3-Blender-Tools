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
    absolute_path: str  # empty while the file only exists inside a bundle


def environment_identifier(depot_path: str) -> str:
    key = str(depot_path or "").replace("/", "\\").casefold()
    return "ENV_" + hashlib.sha1(key.encode("utf-8")).hexdigest()


def _definition_label(parts: tuple[str, ...]) -> str | None:
    """Label for depot path parts, or None when the path is not an environment definition."""
    lower = tuple(part.casefold() for part in parts)
    if not lower or not lower[-1].endswith(".env"):
        return None
    if len(parts) > 2 and lower[:2] == ("environment", "definitions"):
        return str(PureWindowsPath(*parts[2:]))
    if len(parts) > 5 and lower[0] == "dlc" and lower[2:5] == ("data", "environment", "definitions"):
        return str(PureWindowsPath("dlc", parts[1], *parts[5:]))
    return None


def definition_item(depot_path: str, absolute_path: str = "") -> EnvironmentDefinitionItem | None:
    """Item for a vanilla or ``dlc\\<name>\\data`` definition path; None for any other path."""
    depot_path = str(depot_path or "").replace("/", "\\").strip("\\")
    label = _definition_label(PureWindowsPath(depot_path).parts)
    if label is None:
        return None
    return EnvironmentDefinitionItem(
        environment_identifier(depot_path), label, depot_path, str(absolute_path or "")
    )


def _sorted(found: dict[str, EnvironmentDefinitionItem]) -> tuple[EnvironmentDefinitionItem, ...]:
    return tuple(sorted(found.values(), key=lambda item: (item.label.casefold(), item.label)))


def _definition_dirs(root: Path) -> Iterable[Path]:
    yield root.joinpath(*ENVIRONMENT_DEFINITIONS_DEPOT_ROOT.parts)
    try:
        dlc_dirs = sorted(
            (path for path in root.joinpath("dlc").iterdir() if path.is_dir()),
            key=lambda path: path.name.casefold(),
        )
    except OSError:
        return
    for dlc_dir in dlc_dirs:
        yield dlc_dir.joinpath("data", *ENVIRONMENT_DEFINITIONS_DEPOT_ROOT.parts)


def scan_environment_definitions(roots: Iterable[str]) -> tuple[EnvironmentDefinitionItem, ...]:
    """``.env`` files below ``environment\\definitions`` and ``dlc\\*\\data\\environment\\definitions``.

    Roots are ordered by depot priority, so the first copy of a duplicate path wins.
    """

    found: dict[str, EnvironmentDefinitionItem] = {}
    for root_value in roots or ():
        root_text = str(root_value or "").strip()
        if not root_text:
            continue
        root = Path(root_text)
        for definition_root in _definition_dirs(root):
            try:
                if not definition_root.is_dir():
                    continue
            except OSError:
                continue
            for directory, dirnames, filenames in os.walk(definition_root):
                dirnames.sort(key=str.casefold)
                for filename in sorted(filenames, key=str.casefold):
                    absolute_path = Path(directory, filename)
                    try:
                        relative_path = absolute_path.relative_to(root)
                    except ValueError:
                        continue
                    item = definition_item(str(PureWindowsPath(*relative_path.parts)), str(absolute_path))
                    if item is not None:
                        found.setdefault(item.depot_path.casefold(), item)
    return _sorted(found)


def catalog_from_depot_paths(paths: Iterable[str]) -> tuple[EnvironmentDefinitionItem, ...]:
    """Catalog from depot paths such as the bundle index keys; items carry no absolute path."""
    found: dict[str, EnvironmentDefinitionItem] = {}
    for path in paths or ():
        text = str(path)
        if not text.casefold().endswith(".env"):
            continue
        item = definition_item(text)
        if item is not None:
            found.setdefault(item.depot_path.casefold(), item)
    return _sorted(found)


def merge_catalogs(*catalogs: Iterable[EnvironmentDefinitionItem]) -> tuple[EnvironmentDefinitionItem, ...]:
    """Union by depot path; earlier catalogs win."""
    found: dict[str, EnvironmentDefinitionItem] = {}
    for catalog in catalogs:
        for item in catalog:
            found.setdefault(item.depot_path.casefold(), item)
    return _sorted(found)


__all__ = (
    "ENVIRONMENT_DEFINITIONS_DEPOT_ROOT",
    "ENVIRONMENT_OFF_IDENTIFIER",
    "EnvironmentDefinitionItem",
    "catalog_from_depot_paths",
    "definition_item",
    "environment_identifier",
    "merge_catalogs",
    "scan_environment_definitions",
)
