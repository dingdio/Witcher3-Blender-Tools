import functools
import json
import os
from pathlib import Path

from . import plugin_install


def _addon_preferences(context):
    from .. import get_all_addon_prefs

    return get_all_addon_prefs(context)


def _resolve_project_path(path: str) -> Path:
    try:
        import bpy

        path = bpy.path.abspath(path)
    except Exception:
        pass
    return Path(os.path.normpath(path))


@functools.lru_cache(maxsize=1)
def bundled_plugin_version() -> tuple[str, str]:
    """(Version, VersionName) of the plugin shipped inside this add-on -- the
    source that ``Install/Update`` copies when no override is set. Cached: it
    only changes when the add-on itself is updated."""
    try:
        descriptor = Path(plugin_install.default_plugin_source()) / plugin_install.PLUGIN_DESCRIPTOR
        data, error = _read_json(descriptor)
        if error:
            return "", ""
        return (
            str(data.get("Version") or "").strip(),
            str(data.get("VersionName") or "").strip(),
        )
    except Exception:
        return "", ""


def _read_json(path: Path):
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except Exception as exc:
        return {}, str(exc)
    if not isinstance(data, dict):
        return {}, "JSON root must be an object."
    return data, ""


def iter_project_paths(context):
    try:
        prefs = _addon_preferences(context)
    except Exception:
        return []

    items = []
    for index, item in enumerate(getattr(prefs, "unreal_projects", []) or []):
        path = str(getattr(item, "path", "") or "").strip()
        if not path:
            continue
        items.append((index, _resolve_project_path(path)))
    return items


def get_active_project_path(context):
    try:
        prefs = _addon_preferences(context)
    except Exception:
        return None

    projects = getattr(prefs, "unreal_projects", [])
    index = int(getattr(prefs, "unreal_projects_index", 0) or 0)
    if projects and 0 <= index < len(projects):
        path = str(getattr(projects[index], "path", "") or "").strip()
        if path:
            return _resolve_project_path(path)
    return None


def set_active_project_index(context, index) -> bool:
    try:
        prefs = _addon_preferences(context)
    except Exception:
        return False

    projects = getattr(prefs, "unreal_projects", [])
    try:
        index = int(index)
    except (TypeError, ValueError):
        return False
    if projects and 0 <= index < len(projects):
        prefs.unreal_projects_index = index
        return True
    return False


# inspect_project() reads the .uproject and the plugin descriptor off disk and
# JSON-parses both. The Unreal Export panel calls it from draw(), which fires on
# every redraw (mouse-over, region resize, ...), so the raw reads are cached and
# only re-run when either file's mtime/size changes.
_INSPECT_CACHE: dict[str, tuple] = {}
_INSPECT_CACHE_MAX = 32


def _file_signature(path: Path):
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _inspect_signature(project_path: Path) -> tuple:
    descriptor_signature = None
    if project_path.suffix.lower() == ".uproject":
        try:
            target_dir = Path(plugin_install.plugin_target_dir(project_path))
            descriptor_signature = _file_signature(target_dir / plugin_install.PLUGIN_DESCRIPTOR)
        except Exception:
            descriptor_signature = None
    return (_file_signature(project_path), descriptor_signature)


def inspect_project_cached(uproject_path) -> dict:
    """draw()-safe wrapper around inspect_project(): re-reads from disk only when
    the project file or plugin descriptor changes (keyed by mtime + size)."""
    raw_path = str(uproject_path or "").strip()
    if not raw_path:
        return inspect_project(uproject_path)

    key = str(_resolve_project_path(raw_path))
    signature = _inspect_signature(_resolve_project_path(raw_path))
    cached = _INSPECT_CACHE.get(key)
    if cached is not None and cached[0] == signature:
        return cached[1]

    info = inspect_project(uproject_path)
    _INSPECT_CACHE[key] = (signature, info)
    if len(_INSPECT_CACHE) > _INSPECT_CACHE_MAX:
        for stale_key in list(_INSPECT_CACHE)[:-_INSPECT_CACHE_MAX]:
            _INSPECT_CACHE.pop(stale_key, None)
    return info


def inspect_project(uproject_path) -> dict:
    raw_path = str(uproject_path or "").strip()
    info = {
        "project_file": "",
        "exists": False,
        "is_uproject": False,
        "engine_association": "",
        "project_error": "",
        "plugin_target_dir": "",
        "plugin_descriptor": "",
        "plugin_installed": False,
        "plugin_version": "",
        "plugin_version_name": "",
        "plugin_update_available": False,
        "plugin_error": "",
    }
    bundled_version, bundled_version_name = bundled_plugin_version()
    info["bundled_version"] = bundled_version
    info["bundled_version_name"] = bundled_version_name
    if not raw_path:
        info["project_error"] = "Select an Unreal .uproject file."
        return info

    project_path = _resolve_project_path(raw_path)
    info["project_file"] = str(project_path)
    info["is_uproject"] = project_path.suffix.lower() == ".uproject"
    if not info["is_uproject"]:
        info["project_error"] = "Select an Unreal .uproject file."
    info["exists"] = project_path.exists()
    if info["is_uproject"]:
        target_dir = Path(plugin_install.plugin_target_dir(project_path))
        descriptor = target_dir / plugin_install.PLUGIN_DESCRIPTOR
        info["plugin_target_dir"] = str(target_dir)
        info["plugin_descriptor"] = str(descriptor)
        info["plugin_installed"] = target_dir.is_dir() and descriptor.is_file()

    if info["is_uproject"] and info["exists"]:
        project_data, error = _read_json(project_path)
        if error:
            info["project_error"] = f"Could not read project file: {error}"
        else:
            engine = str(project_data.get("EngineAssociation") or "").strip()
            info["engine_association"] = engine or "Unknown"
    elif info["is_uproject"]:
        info["project_error"] = "Project file does not exist."

    if info["plugin_installed"]:
        descriptor_data, error = _read_json(Path(info["plugin_descriptor"]))
        if error:
            info["plugin_error"] = f"Could not read plugin descriptor: {error}"
        else:
            info["plugin_version"] = str(descriptor_data.get("Version") or "").strip()
            info["plugin_version_name"] = str(descriptor_data.get("VersionName") or "").strip()

    if info["plugin_installed"] and (bundled_version_name or bundled_version):
        if bundled_version_name:
            info["plugin_update_available"] = bundled_version_name != info["plugin_version_name"]
        else:
            info["plugin_update_available"] = bundled_version != info["plugin_version"]

    return info


def plugin_status_label(info: dict) -> str:
    if not info.get("plugin_target_dir"):
        return "Not checked"
    if not info.get("plugin_installed"):
        return "Not installed"
    version = info.get("plugin_version_name") or info.get("plugin_version")
    return f"Installed ({version})" if version else "Installed"


def short_project_status(info: dict) -> str:
    if not info.get("project_file"):
        return "Select Unreal project"
    project_error = str(info.get("project_error") or "")
    if project_error:
        if "does not exist" in project_error:
            return "Project file missing"
        if "Select an Unreal" in project_error:
            return "Select Unreal .uproject"
        return "Project file unreadable"
    engine = info.get("engine_association") or "Unknown"
    engine_label = "UE version unknown" if engine == "Unknown" else f"UE {engine}"
    plugin_label = "Plugin installed" if info.get("plugin_installed") else "Plugin missing"
    return f"{engine_label}; {plugin_label}"


def project_status_line(info: dict) -> tuple[str, str]:
    """(label, icon) describing just the project/engine state for the panel."""
    if not info.get("project_file"):
        return "Select an Unreal .uproject file", "INFO"
    error = str(info.get("project_error") or "")
    if error:
        if "does not exist" in error:
            return "Project file not found", "ERROR"
        if "Select an Unreal" in error:
            return "Not a .uproject file", "ERROR"
        return "Project file is unreadable", "ERROR"
    engine = info.get("engine_association") or "Unknown"
    if engine == "Unknown":
        return "Unreal project", "CHECKMARK"
    return f"Unreal Engine {engine}", "CHECKMARK"


def plugin_status_line(info: dict) -> tuple[str, str]:
    """(label, icon) describing the importer plugin's install state."""
    if not info.get("project_file") or not info.get("is_uproject") or not info.get("exists"):
        return "Plugin: select a project first", "DOT"
    if not info.get("plugin_installed"):
        return "Plugin not installed in this project", "ERROR"
    version = info.get("plugin_version_name") or info.get("plugin_version") or "?"
    if info.get("plugin_update_available"):
        bundled = info.get("bundled_version_name") or info.get("bundled_version") or "?"
        return f"Plugin v{version} installed - update to v{bundled} available", "ERROR"
    return f"Plugin v{version} installed (up to date)", "CHECKMARK"


def plugin_action(info: dict) -> tuple[str, str]:
    """(button label, icon) for the install/update operator, reflecting state."""
    if not info.get("plugin_installed"):
        return "Install Plugin", "IMPORT"
    if info.get("plugin_update_available"):
        return "Update Plugin", "FILE_REFRESH"
    return "Reinstall Plugin", "FILE_REFRESH"


def format_project_details(info: dict) -> str:
    lines = [
        f"Project: {info.get('project_file', '')}",
        f"EngineAssociation: {info.get('engine_association') or 'Unknown'}",
        f"Plugin: {plugin_status_label(info)}",
        f"Plugin target: {info.get('plugin_target_dir', '')}",
        f"Plugin descriptor: {info.get('plugin_descriptor', '')}",
    ]
    if info.get("project_error"):
        lines.append(f"Project error: {info['project_error']}")
    if info.get("plugin_error"):
        lines.append(f"Plugin error: {info['plugin_error']}")
    return "\n".join(lines)
