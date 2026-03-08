import ctypes
from ctypes import wintypes
import os
import re
import logging
log = logging.getLogger(__name__)
try:
    from .extension_paths import get_dev_override
except Exception:
    def get_dev_override(_key, default=None):
        return default

WITCHER3_EXE_REL = os.path.join("bin", "x64", "witcher3.exe")
WITCHER2_EXE_REL = os.path.join("bin", "witcher2.exe")


def _normalize_dir(path):
    if not path:
        return ""
    try:
        return os.path.normpath(os.path.abspath(path))
    except Exception:
        return os.path.normpath(path)


def get_witcher3_exe_path(game_path):
    game_root = _normalize_dir(game_path)
    if not game_root:
        return ""
    return os.path.join(game_root, WITCHER3_EXE_REL)


def is_valid_witcher3_game_path(game_path):
    exe_path = get_witcher3_exe_path(game_path)
    return bool(exe_path and os.path.isfile(exe_path))


def get_witcher2_exe_path(game_path):
    game_root = _normalize_dir(game_path)
    if not game_root:
        return ""
    return os.path.join(game_root, WITCHER2_EXE_REL)


def is_valid_witcher2_game_path(game_path):
    exe_path = get_witcher2_exe_path(game_path)
    return bool(exe_path and os.path.isfile(exe_path))


def _iter_common_install_candidates():
    candidates = []
    system_drive = os.environ.get("SystemDrive", "C:")
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")

    gog_names = [
        "The Witcher 3 Wild Hunt",
        "The Witcher 3 Wild Hunt GOTY",
        "The Witcher 3",
    ]
    steam_names = [
        "The Witcher 3 Wild Hunt",
        "The Witcher 3",
    ]

    for name in gog_names:
        candidates.append(os.path.join(system_drive, "GOG Games", name))
        candidates.append(os.path.join(program_files, "GOG Galaxy", "Games", name))
        candidates.append(os.path.join(program_files_x86, "GOG Galaxy", "Games", name))

    steam_roots = [
        os.path.join(program_files_x86, "Steam"),
        os.path.join(program_files, "Steam"),
    ]
    for steam_root in steam_roots:
        for name in steam_names:
            candidates.append(os.path.join(steam_root, "steamapps", "common", name))

    return candidates


def _iter_common_install_candidates_w2():
    candidates = []
    system_drive = os.environ.get("SystemDrive", "C:")
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")

    names = [
        "The Witcher 2",
        "The Witcher 2 Assassins of Kings Enhanced Edition",
        "The Witcher 2: Assassins of Kings Enhanced Edition",
    ]

    for name in names:
        candidates.append(os.path.join(system_drive, "GOG Games", name))
        candidates.append(os.path.join(program_files, "GOG Galaxy", "Games", name))
        candidates.append(os.path.join(program_files_x86, "GOG Galaxy", "Games", name))

    steam_roots = [
        os.path.join(program_files_x86, "Steam"),
        os.path.join(program_files, "Steam"),
    ]
    for steam_root in steam_roots:
        for name in names:
            candidates.append(os.path.join(steam_root, "steamapps", "common", name))

    return candidates


def _iter_registry_install_locations():
    try:
        import winreg
    except Exception:
        return []

    candidates = []

    def _read_values(root, subkey):
        try:
            with winreg.OpenKey(root, subkey) as key:
                idx = 0
                while True:
                    try:
                        name, value, _vtype = winreg.EnumValue(key, idx)
                    except OSError:
                        break
                    idx += 1
                    if isinstance(value, str) and value:
                        yield name, value
        except OSError:
            return

    uninstall_roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]

    for root, base in uninstall_roots:
        try:
            with winreg.OpenKey(root, base) as parent:
                sub_count = winreg.QueryInfoKey(parent)[0]
                for i in range(sub_count):
                    try:
                        sub_name = winreg.EnumKey(parent, i)
                    except OSError:
                        continue
                    sub_path = f"{base}\\{sub_name}"
                    values = {k: v for k, v in _read_values(root, sub_path)}
                    display_name = str(values.get("DisplayName", "")).lower()
                    if not display_name:
                        continue
                    if "witcher" not in display_name or "3" not in display_name:
                        continue
                    install_location = values.get("InstallLocation", "")
                    if install_location:
                        candidates.append(install_location)
                    display_icon = values.get("DisplayIcon", "")
                    if display_icon:
                        display_icon = str(display_icon).split(",", 1)[0].strip().strip('"')
                        candidates.append(display_icon)
        except OSError:
            continue

    steam_paths = []
    for root, subkey in (
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Valve\Steam"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
    ):
        for key_name, value in _read_values(root, subkey):
            if key_name.lower() in {"steampath", "installdir"}:
                steam_paths.append(value)

    for steam_path in steam_paths:
        candidates.append(steam_path)

    return candidates


def _iter_registry_install_locations_w2():
    try:
        import winreg
    except Exception:
        return []

    candidates = []

    def _read_values(root, subkey):
        try:
            with winreg.OpenKey(root, subkey) as key:
                idx = 0
                while True:
                    try:
                        name, value, _vtype = winreg.EnumValue(key, idx)
                    except OSError:
                        break
                    idx += 1
                    if isinstance(value, str) and value:
                        yield name, value
        except OSError:
            return

    uninstall_roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]

    for root, base in uninstall_roots:
        try:
            with winreg.OpenKey(root, base) as parent:
                sub_count = winreg.QueryInfoKey(parent)[0]
                for i in range(sub_count):
                    try:
                        sub_name = winreg.EnumKey(parent, i)
                    except OSError:
                        continue
                    sub_path = f"{base}\\{sub_name}"
                    values = {k: v for k, v in _read_values(root, sub_path)}
                    display_name = str(values.get("DisplayName", "")).lower()
                    if not display_name:
                        continue
                    if "witcher" not in display_name or "2" not in display_name:
                        continue
                    install_location = values.get("InstallLocation", "")
                    if install_location:
                        candidates.append(install_location)
                    display_icon = values.get("DisplayIcon", "")
                    if display_icon:
                        display_icon = str(display_icon).split(",", 1)[0].strip().strip('"')
                        candidates.append(display_icon)
        except OSError:
            continue

    return candidates


def _parse_steam_libraryfolders_vdf(vdf_path):
    libraries = []
    if not os.path.isfile(vdf_path):
        return libraries
    try:
        with open(vdf_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                match = re.search(r'"path"\s*"([^"]+)"', line)
                if not match:
                    continue
                lib_path = match.group(1).replace("\\\\", "\\")
                if lib_path:
                    libraries.append(lib_path)
    except Exception:
        return libraries
    return libraries


def _iter_steam_library_candidates_for_names(folder_names):
    candidates = []
    steam_roots = []

    for raw in _iter_registry_install_locations():
        raw_norm = _normalize_dir(raw)
        if not raw_norm:
            continue
        if raw_norm.lower().endswith("steam.exe"):
            raw_norm = os.path.dirname(raw_norm)
        if os.path.basename(raw_norm).lower() == "steam":
            steam_roots.append(raw_norm)

    # Also include common roots in case registry probing misses Steam.
    for path in _iter_common_install_candidates():
        norm = _normalize_dir(path)
        if not norm:
            continue
        if "\\steamapps\\common\\" in norm.lower():
            steam_roots.append(norm.split("\\steamapps\\common\\", 1)[0])

    seen = set()
    for steam_root in steam_roots:
        steam_root = _normalize_dir(steam_root)
        if not steam_root or steam_root.lower() in seen:
            continue
        seen.add(steam_root.lower())
        vdf = os.path.join(steam_root, "steamapps", "libraryfolders.vdf")
        for lib in [steam_root, *_parse_steam_libraryfolders_vdf(vdf)]:
            lib_norm = _normalize_dir(lib)
            if not lib_norm:
                continue
            for folder_name in folder_names:
                candidates.append(os.path.join(lib_norm, "steamapps", "common", folder_name))
    return candidates


def _iter_steam_library_candidates():
    return _iter_steam_library_candidates_for_names(("The Witcher 3 Wild Hunt", "The Witcher 3"))


def _iter_steam_library_candidates_w2():
    return _iter_steam_library_candidates_for_names((
        "The Witcher 2",
        "The Witcher 2 Assassins of Kings Enhanced Edition",
        "The Witcher 2: Assassins of Kings Enhanced Edition",
    ))


def auto_detect_witcher3_game_path():
    candidates = []
    candidates.extend(_iter_common_install_candidates())
    candidates.extend(_iter_registry_install_locations())
    candidates.extend(_iter_steam_library_candidates())

    # De-duplicate while preserving priority order.
    seen = set()
    for candidate in candidates:
        if not candidate:
            continue
        candidate_norm = _normalize_dir(candidate)
        if not candidate_norm:
            continue

        # Registry DisplayIcon may point directly to the executable.
        if candidate_norm.lower().endswith("witcher3.exe"):
            maybe_root = _normalize_dir(os.path.dirname(os.path.dirname(os.path.dirname(candidate_norm))))
        else:
            maybe_root = candidate_norm

        key = maybe_root.lower()
        if key in seen:
            continue
        seen.add(key)
        if is_valid_witcher3_game_path(maybe_root):
            return maybe_root
    return ""


def auto_detect_witcher2_game_path():
    candidates = []
    candidates.extend(_iter_common_install_candidates_w2())
    candidates.extend(_iter_registry_install_locations_w2())
    candidates.extend(_iter_steam_library_candidates_w2())

    seen = set()
    for candidate in candidates:
        if not candidate:
            continue
        candidate_norm = _normalize_dir(candidate)
        if not candidate_norm:
            continue
        if candidate_norm.lower().endswith("witcher2.exe"):
            maybe_root = _normalize_dir(os.path.dirname(os.path.dirname(candidate_norm)))
        else:
            maybe_root = candidate_norm
        key = maybe_root.lower()
        if key in seen:
            continue
        seen.add(key)
        if is_valid_witcher2_game_path(maybe_root):
            return maybe_root
    return ""


def _refresh_archive_configuration(game_path):
    """Update the cache-layer global configuration after path changes."""
    try:
        from .CR2W.witcher_cache.common_cache.WitcherArchiveManager import Configuration
    except Exception:
        return

    game_root = _normalize_dir(game_path)
    Configuration.ExecutablePath = game_root
    Configuration.GameModDir = os.path.join(game_root, "mods") if game_root else ""
    Configuration.GameDlcDir = os.path.join(game_root, "dlc") if game_root else ""

def get_translation(buf):
    # Query the Translation table to get (lang, codepage)
    lpdw = wintypes.DWORD()
    lplp = ctypes.c_void_p()
    if not ctypes.windll.version.VerQueryValueW(buf, "\\VarFileInfo\\Translation",
                                               ctypes.byref(lplp), ctypes.byref(lpdw)):
        raise RuntimeError("No translation block")
    # It's an array of WORD pairs (lang, codepage)
    arr = ctypes.cast(lplp, ctypes.POINTER(wintypes.WORD * 2)).contents
    return arr[0], arr[1]

def query_string(buf, lang, codepage, name):
    sub_block = f"\\StringFileInfo\\{lang:04x}{codepage:04x}\\{name}"
    lpdw = wintypes.DWORD()
    lplp = ctypes.c_void_p()
    if ctypes.windll.version.VerQueryValueW(buf, sub_block,
                                            ctypes.byref(lplp), ctypes.byref(lpdw)):
        # lpdw = number of WCHARs
        return ctypes.wstring_at(lplp, lpdw.value)
    return None

def find_p4cl(file_path):
    # Read the entire file in binary mode
    with open(file_path, 'rb') as f:
        data = f.read()
    # Search for "P4CL: " followed by digits (ASCII)
    p4cl_pattern = rb'P4CL: (\d+)'  # Use raw byte string to avoid escape issues
    p4cl_match = re.search(p4cl_pattern, data)
    if p4cl_match:
        # Return the changelist number and the file offset
        return p4cl_match.group(1).decode('ascii'), p4cl_match.start()
    return None, None

def get_all_version_info(path):
    # Load the raw version info resource
    size = ctypes.windll.version.GetFileVersionInfoSizeW(path, None)
    if size == 0:
        raise RuntimeError("No version info found")
    buf = ctypes.create_string_buffer(size)
    if not ctypes.windll.version.GetFileVersionInfoW(path, 0, size, buf):
        raise RuntimeError("Failed to load version info")

    lang, codepage = get_translation(buf)
    fields = ["FileVersion", "ProductVersion", "CompanyName",
              "InternalName", "Comments", "PrivateBuild"]
    info = {}
    for name in fields:
        val = query_string(buf, lang, codepage, name)
        if val:
            info[name] = val
    # Search for P4CL in the entire file
    p4cl, p4cl_offset = find_p4cl(path)
    if p4cl:
        info["P4CL"] = (p4cl, p4cl_offset)
    return info

def update_witcher_game_path(self, context):
    """Update callback for witcher_game_path."""
    game_path = _normalize_dir(getattr(self, "witcher_game_path", ""))
    if getattr(self, "witcher_game_path", "") != game_path:
        # Normalize persisted path for cleaner comparisons/UX.
        self.witcher_game_path = game_path
        return

    _refresh_archive_configuration(game_path)

    exe_path = get_witcher3_exe_path(game_path)
    if os.path.isfile(exe_path):
        version_info = get_all_version_info(exe_path)
        if "Error" in version_info:
            self.version_info = version_info["Error"]
        else:
            # Format version info for display
            lines = [f"{k}: {v[0] if k == 'P4CL' else v}" + 
                     (f" (Offset: {hex(v[1])})" if k == "P4CL" else "") 
                     for k, v in version_info.items()]
            self.version_info = "\n".join(lines)
    else:
        if game_path:
            self.version_info = "Error: invalid Witcher 3 path (missing bin\\x64\\witcher3.exe)"
        else:
            self.version_info = "Error: Witcher 3 path not set (choose folder containing bin\\x64\\witcher3.exe)"

if __name__ == "__main__":
    path = get_dev_override("read_game_exe_file", "")
    if not path:
        log.info("Set dev override 'read_game_exe_file' to test this script.")
        raise SystemExit(0)
    info = get_all_version_info(path)
    for k, v in info.items():
        if k == "P4CL":
            p4cl, offset = v
            log.info("%s: %s (File Offset: %s)", k, p4cl, hex(offset))
        else:
            log.info("%s: %s", k, v)
            
# FileVersion: 4.0.0.288741(Build Machine)
# ProductVersion: 4.0.0.288741(Build Machine)
# CompanyName: CD Projekt Red
# InternalName: The Witcher 3
# P4CL: 9412422 (File Offset: 0x22bb86b)
