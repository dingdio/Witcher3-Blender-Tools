import logging
import os
from dataclasses import dataclass, field
from pathlib import Path


log = logging.getLogger(__name__)

_DLC_ENTITY_APPEARANCE_MOUNTER_TYPE = "CR4EntityExternalAppearanceDLCMounter"
_DLC_ENTITY_APPEARANCE_ENTRY_TYPE = "CR4EntityExternalAppearanceDLC"
_DLC_ENTITY_TEMPLATE_PARAM_MOUNTER_TYPE = "CR4EntityTemplateParamDLCMounter"
_DLC_ANIM_TEMPLATE_PARAM_TYPES = {"CAnimAnimsetsParam", "CAnimMimicParam"}


def _norm_fs_path(path: str) -> str:
    try:
        return os.path.normcase(os.path.normpath(str(path or "")))
    except Exception:
        return str(path or "").replace("/", "\\").lower()


def _cr2w_prop_string(prop) -> str:
    if prop is None:
        return ""
    try:
        value = prop.ToString()
    except Exception:
        value = None
    if hasattr(value, "value"):
        value = value.value
    return str(value or "").strip()


def _cr2w_string_array(prop) -> list[str]:
    values = []
    for element in getattr(prop, "elements", None) or []:
        value = ""
        try:
            value = element.ToString()
        except Exception:
            value = getattr(element, "String", None)
        value = str(value or "").strip()
        if value:
            values.append(value.replace("/", "\\"))
    if values:
        return values
    for item in getattr(prop, "More", None) or []:
        value = _cr2w_prop_string(item)
        if value:
            values.append(value.replace("/", "\\"))
    return values


def _cr2w_handle_path(prop) -> str:
    for handle in getattr(prop, "Handles", None) or []:
        value = str(getattr(handle, "DepotPath", "") or "").strip()
        if value:
            return value.replace("/", "\\")
    value = _cr2w_prop_string(prop)
    return value.replace("/", "\\") if value else ""


def _cr2w_handle_paths(prop) -> list[str]:
    values = []
    seen = set()

    def _add(value):
        value = str(value or "").replace("/", "\\").strip()
        if not value:
            return
        key = value.lower()
        if key in seen:
            return
        seen.add(key)
        values.append(value)

    for handle in getattr(prop, "Handles", None) or []:
        _add(getattr(handle, "DepotPath", "") or "")
    for element in getattr(prop, "elements", None) or []:
        _add(getattr(element, "DepotPath", "") or getattr(element, "path", "") or "")
    for item in getattr(prop, "More", None) or []:
        _add(getattr(item, "DepotPath", "") or getattr(item, "path", "") or "")
    if not values:
        for value in _cr2w_string_array(prop):
            _add(value)
    return values


def _iter_cr2w_ptr_chunks(prop, chunks):
    for value in getattr(prop, "value", None) or []:
        try:
            ptr = int(value)
        except Exception:
            continue
        if 1 <= ptr <= len(chunks):
            yield chunks[ptr - 1]
    for handle in getattr(prop, "Handles", None) or []:
        ref = getattr(handle, "Reference", None)
        if isinstance(ref, int) and 0 <= ref < len(chunks):
            yield chunks[ref]
            continue
        ptr = getattr(handle, "val", None)
        if isinstance(ptr, int) and 1 <= ptr <= len(chunks):
            yield chunks[ptr - 1]


def _cr2w_prop_bool(prop, default=False) -> bool:
    if prop is None:
        return bool(default)
    value = getattr(prop, "Value", None)
    if value is None:
        value = getattr(prop, "value", None)
    if value is None:
        value = _cr2w_prop_string(prop)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes", "on"}:
        return True
    if text in {"false", "0", "no", "off"}:
        return False
    return bool(default)


def _is_under_root_path(path: str, root: str) -> bool:
    if not path or not root:
        return False
    try:
        path_norm = os.path.normcase(os.path.abspath(os.path.normpath(path)))
        root_norm = os.path.normcase(os.path.abspath(os.path.normpath(root)))
        return path_norm == root_norm or path_norm.startswith(root_norm.rstrip("\\/") + os.sep)
    except Exception:
        path_norm = _norm_fs_path(path)
        root_norm = _norm_fs_path(root).rstrip("\\/")
        return bool(path_norm and root_norm and (path_norm == root_norm or path_norm.startswith(root_norm + "\\")))


def _repo_path_from_abs(path: str, roots=None) -> str:
    path = str(path or "").strip()
    if not path:
        return ""
    normalized = os.path.normpath(path)
    if not os.path.isabs(normalized):
        return normalized.replace("/", "\\").lstrip("\\")

    matching_roots = []
    for root in roots or []:
        if root and _is_under_root_path(normalized, root):
            matching_roots.append(os.path.normpath(root))
    matching_roots.sort(key=len, reverse=True)
    for root in matching_roots:
        try:
            rel = os.path.relpath(normalized, root)
        except Exception:
            continue
        if rel and rel != ".":
            return rel.replace("/", "\\").lstrip("\\")

    return normalized.replace("/", "\\").lstrip("\\")


def _dlc_repo_path_key(path: str, roots=None) -> str:
    return _repo_path_from_abs(path, roots).replace("/", "\\").strip().lstrip("\\").lower()


def _dlc_appearance_name_from_path(path: str) -> str:
    stem = Path(str(path or "").replace("\\", "/")).stem
    return str(stem or "").strip()


def _parse_anim_template_param_chunk(param_chunk, reddlc_path: str) -> dict | None:
    param_type = str(getattr(param_chunk, "Type", "") or getattr(param_chunk, "name", "") or "").strip()
    if param_type not in _DLC_ANIM_TEMPLATE_PARAM_TYPES:
        return None

    animsets = _cr2w_handle_paths(param_chunk.GetVariableByName("animationSets"))
    if not animsets:
        return None

    name = _cr2w_prop_string(param_chunk.GetVariableByName("name"))
    if not name and param_type == "CAnimMimicParam":
        name = "MimicSets"

    return {
        "param_type": param_type,
        "name": name,
        "componentName": _cr2w_prop_string(param_chunk.GetVariableByName("componentName")),
        "animationSets": animsets,
        "reddlc_path": reddlc_path,
    }


@dataclass
class CDLCDefinition:
    key: str = ""
    id: str = ""
    name: str = ""
    description: str = ""
    name_key: str = ""
    description_key: str = ""
    folder_name: str = ""
    source_id: str = ""
    source_label: str = ""
    source_kind: str = ""
    source_order: int = 0
    root_path: str = ""
    dlc_dir: str = ""
    reddlc_path: str = ""
    is_vanilla: bool = False
    initially_enabled: bool = True
    visible_in_dlc_menu: bool = True
    required_by_game_save: bool = False
    enabled: bool = True
    mounter_types: list[str] = field(default_factory=list)
    appearance_mounters: dict[str, list[dict]] = field(default_factory=dict)
    template_param_mounters: dict[str, list[dict]] = field(default_factory=dict)

    @classmethod
    def from_file(
        cls,
        reddlc_path: str,
        source: dict,
        repo_roots=None,
        parent_name: str = "",
        display_reddlc_path: str = "",
    ):
        display_path = str(display_reddlc_path or reddlc_path or "").replace("/", "\\").strip()
        data = cls._read_resource_data(reddlc_path, repo_roots)
        for table_name in ("appearance_mounters", "template_param_mounters"):
            for entries in (data.get(table_name, {}) or {}).values():
                for entry in entries or []:
                    entry["reddlc_path"] = display_path or reddlc_path
        path_key = _norm_fs_path(display_path or reddlc_path)
        dlc_id = data.get("id") or parent_name or Path(reddlc_path).stem
        name_key = data.get("localized_name_key") or ""
        description_key = data.get("localized_description_key") or ""
        name = name_key or parent_name or dlc_id
        description = description_key
        parent_dir = os.path.dirname(display_path or reddlc_path)
        folder_name = os.path.basename(os.path.normpath(parent_dir)) or parent_name or Path(display_path or reddlc_path).stem
        source_kind = str(source.get("source_kind", "") or "").strip()
        vanilla_names = {str(name or "").lower() for name in source.get("vanilla_dlc_names", ()) or ()}
        source_id = str(source.get("source_id", "") or "")
        is_vanilla = bool(source_id == "bundles_uncook" and folder_name.lower() in vanilla_names)
        return cls(
            key=f"{source['source_id']}:{path_key}",
            id=dlc_id,
            name=name,
            description=description,
            name_key=name_key,
            description_key=description_key,
            folder_name=folder_name,
            source_id=source["source_id"],
            source_label=source["source_label"],
            source_kind=source_kind,
            source_order=int(source.get("source_order", 0) or 0),
            root_path=source["root_path"],
            dlc_dir=parent_dir,
            reddlc_path=os.path.normpath(display_path or reddlc_path),
            is_vanilla=is_vanilla,
            initially_enabled=bool(data.get("initially_enabled", True)),
            visible_in_dlc_menu=bool(data.get("visible_in_dlc_menu", True)),
            required_by_game_save=bool(data.get("required_by_game_save", False)),
            enabled=bool(data.get("initially_enabled", True)),
            mounter_types=list(data.get("mounter_types", []) or []),
            appearance_mounters=data.get("appearance_mounters", {}) or {},
            template_param_mounters=data.get("template_param_mounters", {}) or {},
        )

    @staticmethod
    def _read_resource_data(reddlc_path: str, repo_roots=None) -> dict:
        repo_roots = [
            os.path.normpath(str(root or ""))
            for root in repo_roots or []
            if str(root or "").strip()
        ]
        repo_root = repo_roots[0] if repo_roots else ""
        data = {
            "id": "",
            "localized_name_key": "",
            "localized_description_key": "",
            "initially_enabled": True,
            "visible_in_dlc_menu": True,
            "required_by_game_save": False,
            "mounter_types": [],
            "appearance_mounters": {},
            "template_param_mounters": {},
        }
        try:
            from ...CR2W_file import read_CR2W

            cr2w_file = read_CR2W(reddlc_path)
        except Exception:
            log.debug("Failed to read DLC definition: %s", reddlc_path, exc_info=True)
            return data

        chunks = list(getattr(getattr(cr2w_file, "CHUNKS", None), "CHUNKS", None) or [])
        seen_mounters = set()
        for chunk in chunks:
            chunk_type = str(getattr(chunk, "Type", "") or "")
            if chunk_type == "CDLCDefinition":
                data["id"] = _cr2w_prop_string(chunk.GetVariableByName("id"))
                data["localized_name_key"] = _cr2w_prop_string(chunk.GetVariableByName("localizedNameKey"))
                data["localized_description_key"] = _cr2w_prop_string(chunk.GetVariableByName("localizedDescriptionKey"))
                data["initially_enabled"] = _cr2w_prop_bool(chunk.GetVariableByName("initiallyEnabled"), True)
                data["visible_in_dlc_menu"] = _cr2w_prop_bool(chunk.GetVariableByName("visibleInDLCMenu"), True)
                data["required_by_game_save"] = _cr2w_prop_bool(chunk.GetVariableByName("requiredByGameSave"), False)
            elif chunk_type.endswith("DLCMounter"):
                if chunk_type not in seen_mounters:
                    seen_mounters.add(chunk_type)
                    data["mounter_types"].append(chunk_type)

            if chunk_type == _DLC_ENTITY_APPEARANCE_MOUNTER_TYPE:
                template_paths = _cr2w_string_array(chunk.GetVariableByName("entityTemplatePaths"))
                if not template_paths:
                    continue

                entries = []
                for entry_chunk in _iter_cr2w_ptr_chunks(chunk.GetVariableByName("entityExternalAppearances"), chunks):
                    if getattr(entry_chunk, "Type", None) != _DLC_ENTITY_APPEARANCE_ENTRY_TYPE:
                        continue
                    replacement_name = (
                        _cr2w_prop_string(entry_chunk.GetVariableByName("appearanceToRepleace"))
                        or _cr2w_prop_string(entry_chunk.GetVariableByName("appearanceToReplace"))
                    )
                    w3app_path = _cr2w_handle_path(entry_chunk.GetVariableByName("entityExternalAppearance"))
                    if not w3app_path or not w3app_path.lower().endswith(".w3app"):
                        continue
                    entries.append({
                        "replacement_name": replacement_name,
                        "appearance_name": _dlc_appearance_name_from_path(w3app_path),
                        "w3app_path": w3app_path,
                        "reddlc_path": reddlc_path,
                        "repo_root": repo_root,
                        "repo_roots": list(repo_roots),
                    })
                if not entries:
                    continue

                for template_path in template_paths:
                    key = _dlc_repo_path_key(template_path, repo_roots)
                    if key:
                        data["appearance_mounters"].setdefault(key, []).extend(entries)
            elif chunk_type == _DLC_ENTITY_TEMPLATE_PARAM_MOUNTER_TYPE:
                template_paths = _cr2w_string_array(chunk.GetVariableByName("entityTemplatePaths"))
                if not template_paths:
                    continue

                entries = []
                for param_chunk in _iter_cr2w_ptr_chunks(chunk.GetVariableByName("entityTemplateParams"), chunks):
                    entry = _parse_anim_template_param_chunk(param_chunk, reddlc_path)
                    if entry:
                        entries.append(entry)
                if not entries:
                    continue

                for template_path in template_paths:
                    key = _dlc_repo_path_key(template_path, repo_roots)
                    if key:
                        data["template_param_mounters"].setdefault(key, []).extend(entries)
        return data

    def to_ui_dict(self) -> dict:
        return {
            "key": self.key,
            "source_id": self.source_id,
            "source_label": self.source_label,
            "source_kind": self.source_kind,
            "is_vanilla": self.is_vanilla,
            "dlc_id": self.id,
            "dlc_name": self.name,
            "dlc_description": self.description,
            "dlc_name_key": self.name_key,
            "dlc_description_key": self.description_key,
            "dlc_folder_name": self.folder_name,
            "root_path": self.root_path,
            "dlc_dir": self.dlc_dir,
            "reddlc_path": self.reddlc_path,
            "enabled": self.enabled,
            "mounter_types": ", ".join(self.mounter_types or []),
        }


DLCDefinition = CDLCDefinition
