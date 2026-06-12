"""Helpers for installing the Unreal importer plugin into a UE project."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

PLUGIN_NAME = "WitcherToolsImporter"
PLUGIN_DESCRIPTOR = f"{PLUGIN_NAME}.uplugin"

TRANSIENT_PATTERNS = (
    ".git",
    ".vs",
    "__pycache__",
    "*.pyc",
    "Binaries",
    "DerivedDataCache",
    "Intermediate",
    "Saved",
)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_plugin_source() -> str:
    return str(repo_root() / "Unreal" / PLUGIN_NAME)


def plugin_target_dir(uproject_path: str | os.PathLike[str]) -> str:
    project_file = Path(uproject_path).expanduser()
    return str(project_file.parent / "Plugins" / PLUGIN_NAME)


def install_or_update_plugin(
    uproject_path: str | os.PathLike[str],
    source_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    project_file = Path(uproject_path).expanduser()
    source_path = Path(source_dir or default_plugin_source()).expanduser()

    if project_file.suffix.lower() != ".uproject":
        raise ValueError("Select the Unreal .uproject file.")
    if not project_file.exists():
        raise FileNotFoundError(f"Unreal project file does not exist: {project_file}")
    if not source_path.exists():
        raise FileNotFoundError(f"Plugin source does not exist: {source_path}")
    if not (source_path / PLUGIN_DESCRIPTOR).exists():
        raise FileNotFoundError(f"Plugin source is missing {PLUGIN_DESCRIPTOR}: {source_path}")

    target_path = Path(plugin_target_dir(project_file))
    plugins_dir = project_file.parent / "Plugins"
    if target_path.name != PLUGIN_NAME or target_path.parent != plugins_dir:
        raise ValueError("Resolved plugin target is not inside the project's Plugins folder.")
    if source_path.resolve() == target_path.resolve():
        return {
            "project_file": str(project_file),
            "source_dir": str(source_path),
            "target_dir": str(target_path),
            "updated": False,
            "file_count": _count_files(target_path) if target_path.exists() else 0,
            "message": "Plugin is already in this project.",
        }

    plugins_dir.mkdir(parents=True, exist_ok=True)
    if target_path.exists():
        if not (target_path / PLUGIN_DESCRIPTOR).exists():
            raise ValueError(f"Refusing to overwrite unexpected folder: {target_path}")
        shutil.rmtree(target_path)
    shutil.copytree(source_path, target_path, ignore=shutil.ignore_patterns(*TRANSIENT_PATTERNS))

    return {
        "project_file": str(project_file),
        "source_dir": str(source_path),
        "target_dir": str(target_path),
        "updated": True,
        "file_count": _count_files(target_path),
        "message": "Plugin installed.",
    }


def format_install_details(result: dict[str, Any]) -> str:
    return "\n".join(
        [
            result.get("message", "Plugin installed."),
            f"Project: {result.get('project_file', '')}",
            f"Source: {result.get('source_dir', '')}",
            f"Target: {result.get('target_dir', '')}",
            f"Files: {result.get('file_count', 0)}",
        ]
    )


def _count_files(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for item in path.rglob("*") if item.is_file())
