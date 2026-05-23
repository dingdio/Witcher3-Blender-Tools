import logging
import hashlib
import os
import pickle
import time

from .. import cache_meta
from ..common_cache.WitcherArchiveManager import WitcherArchiveManager
from ....extension_paths import get_cache_root
from .CDLCDefinition import CDLCDefinition, DLCDefinition


log = logging.getLogger(__name__)


def _norm_fs_path(path: str) -> str:
    try:
        return os.path.normcase(os.path.normpath(str(path or "")))
    except Exception:
        return str(path or "").replace("/", "\\").lower()


def _dedupe_roots(paths):
    out = []
    seen = set()
    for path in paths or []:
        path = str(path or "").strip()
        if not path:
            continue
        norm = os.path.normpath(path)
        key = _norm_fs_path(norm)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(norm)
    return out


def _candidate_dlc_roots(root: str, rel_paths=()):
    root = str(root or "").strip()
    if not root:
        return []
    root = os.path.normpath(root)
    candidates = []
    if os.path.isdir(root) and os.path.basename(root.rstrip("\\/")).lower() == "dlc":
        candidates.append(root)
    for rel_path in rel_paths or ("dlc",):
        candidate = os.path.join(root, rel_path)
        if os.path.isdir(candidate):
            candidates.append(candidate)
    return _dedupe_roots(candidates)


def _iter_reddlc_files_one_level(dlc_root: str):
    dlc_root = str(dlc_root or "").strip()
    if not dlc_root or not os.path.isdir(dlc_root):
        return

    try:
        root_entries = list(os.scandir(dlc_root))
    except Exception:
        return

    for entry in root_entries:
        try:
            if entry.is_file() and entry.name.lower().endswith(".reddlc"):
                yield entry.path
        except Exception:
            continue

    for dlc_dir in root_entries:
        try:
            if not dlc_dir.is_dir():
                continue
            for file_entry in os.scandir(dlc_dir.path):
                if file_entry.is_file() and file_entry.name.lower().endswith(".reddlc"):
                    yield file_entry.path
        except Exception:
            continue


def _virtual_assets_mod_reddlc_path(path: str) -> str:
    path = str(path or "").replace("/", "\\").strip("\\")
    parts = [part for part in path.split("\\") if part]
    if len(parts) == 4 and parts[1].lower() == "dlc" and parts[-1].lower().endswith(".reddlc"):
        return "\\".join(parts[1:])
    return ""


def _bundle_item_signature(virtual_path: str, item) -> str:
    bundle = getattr(item, "bundle", None) or getattr(item, "Bundle", None)
    archive_path = str(getattr(bundle, "ArchiveAbsolutePath", "") or "")
    parts = [
        str(virtual_path or ""),
        archive_path,
        str(getattr(item, "size", "") or ""),
        str(getattr(item, "zsize", "") or ""),
        str(getattr(item, "timestamp", "") or ""),
        str(getattr(item, "crc", "") or ""),
    ]
    return "|".join(parts)


def _extract_bundle_reddlc_item(virtual_path: str, item) -> str:
    cache_dir = os.path.join(get_cache_root(create=True), "DLC", "assets_mods")
    os.makedirs(cache_dir, exist_ok=True)
    digest = hashlib.sha1(_bundle_item_signature(virtual_path, item).encode("utf-8", "ignore")).hexdigest()
    extract_path = os.path.join(cache_dir, f"{digest}.reddlc")
    if not os.path.exists(extract_path):
        item.extract_to_file(extract_path)
    return extract_path


def _iter_assets_mod_reddlc_files(source: dict):
    try:
        from ..Bundles import LoadBundleManager

        manager = LoadBundleManager(loadmods=True, reset_cache=True)
    except Exception:
        log.debug("Failed to load assets mod bundle cache for DLC definitions.", exc_info=True)
        return

    for virtual_path, items in getattr(manager, "Items", {}).items():
        depot_path = _virtual_assets_mod_reddlc_path(virtual_path)
        if not depot_path:
            continue
        final_item = items[-1] if isinstance(items, list) and items else items
        if final_item is None:
            continue
        try:
            extract_path = _extract_bundle_reddlc_item(virtual_path, final_item)
        except Exception:
            log.debug("Failed to extract assets mod DLC definition: %s", virtual_path, exc_info=True)
            continue
        parts = depot_path.split("\\")
        parent_name = parts[1] if len(parts) >= 3 else os.path.splitext(os.path.basename(depot_path))[0]
        yield extract_path, depot_path, parent_name


def _merge_mounter_table(target: dict, parsed: dict):
    for key, entries in (parsed or {}).items():
        if not key or not entries:
            continue
        target.setdefault(key, []).extend(entries)


def _dedupe_appearance_table(table: dict):
    for key, entries in list((table or {}).items()):
        deduped = []
        seen_entries = set()
        for entry in entries:
            entry_key = (
                str(entry.get("replacement_name", "") or "").lower(),
                str(entry.get("appearance_name", "") or "").lower(),
                _norm_fs_path(entry.get("w3app_path", "")),
                _norm_fs_path(entry.get("reddlc_path", "")),
            )
            if entry_key in seen_entries:
                continue
            seen_entries.add(entry_key)
            deduped.append(entry)
        table[key] = deduped


def _dedupe_template_param_table(table: dict):
    for key, entries in list((table or {}).items()):
        deduped = []
        seen_entries = set()
        for entry in entries:
            entry_key = (
                str(entry.get("param_type", "") or "").lower(),
                str(entry.get("name", "") or "").lower(),
                str(entry.get("componentName", "") or "").lower(),
                tuple(str(path or "").lower() for path in entry.get("animationSets", []) or []),
                _norm_fs_path(entry.get("reddlc_path", "")),
            )
            if entry_key in seen_entries:
                continue
            seen_entries.add(entry_key)
            deduped.append(entry)
        table[key] = deduped


class DLCManager:
    InstanceManager = None

    def __init__(self):
        self.source_roots = []
        self.definitions: list[DLCDefinition] = []
        self.mounted_content: list[DLCDefinition] = []

    @staticmethod
    def SerializationVersion():
        return "1.6"

    @staticmethod
    def _normalized_source_roots(source_roots=None) -> list[dict]:
        out = []
        seen = set()
        for index, source in enumerate(source_roots or []):
            raw_root_path = str(source.get("root_path", "") or "").strip()
            if not raw_root_path:
                continue
            root_path = os.path.normpath(raw_root_path)
            source_id = str(source.get("source_id", "") or "")
            source_type = str(source.get("source_type", "") or "disk")
            source_kind = str(source.get("source_kind", "") or "")
            rel_paths = tuple(str(path or "") for path in (source.get("rel_paths", None) or ("dlc",)))
            repo_roots = tuple(
                os.path.normpath(str(path or ""))
                for path in (source.get("repo_roots", None) or ())
                if str(path or "").strip()
            )
            vanilla_dlc_names = tuple(source.get("vanilla_dlc_names", None) or WitcherArchiveManager.VANILLA_DLC_LIST)
            key = (source_id, source_type, _norm_fs_path(root_path), rel_paths, repo_roots)
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "source_id": source_id,
                "source_label": str(source.get("source_label", "") or source_id or "DLC"),
                "source_type": source_type,
                "source_kind": source_kind,
                "source_order": index,
                "root_path": root_path,
                "rel_paths": rel_paths,
                "repo_roots": repo_roots,
                "vanilla_dlc_names": vanilla_dlc_names,
            })
        return out

    @staticmethod
    def DiscoverDefinitions(source_roots=None) -> list[DLCDefinition]:
        definitions = []
        seen_paths = set()
        for source in DLCManager._normalized_source_roots(source_roots):
            if source.get("source_type") == "assets_mods":
                repo_roots = _dedupe_roots((*source.get("repo_roots", ()), source["root_path"]))
                for reddlc_path, display_path, parent_name in _iter_assets_mod_reddlc_files(source):
                    path_key = f"{source['source_id']}:{_norm_fs_path(display_path)}"
                    if not path_key or path_key in seen_paths:
                        continue
                    seen_paths.add(path_key)
                    definitions.append(CDLCDefinition.from_file(
                        reddlc_path,
                        source,
                        repo_roots=repo_roots,
                        parent_name=parent_name,
                        display_reddlc_path=display_path,
                    ))
                continue
            for dlc_root in _candidate_dlc_roots(source["root_path"], source["rel_paths"]):
                for reddlc_path in _iter_reddlc_files_one_level(dlc_root):
                    path_key = _norm_fs_path(reddlc_path)
                    if not path_key or path_key in seen_paths:
                        continue
                    seen_paths.add(path_key)

                    parent_dir = os.path.dirname(reddlc_path)
                    parent_name = os.path.basename(parent_dir)
                    if _norm_fs_path(parent_dir) == _norm_fs_path(dlc_root):
                        parent_name = os.path.splitext(os.path.basename(reddlc_path))[0]

                    repo_roots = _dedupe_roots((*source.get("repo_roots", ()), os.path.dirname(dlc_root), source["root_path"]))
                    definitions.append(CDLCDefinition.from_file(
                        reddlc_path,
                        source,
                        repo_roots=repo_roots,
                        parent_name=parent_name,
                    ))
        definitions.sort(key=lambda item: (
            item.source_order,
            item.folder_name.casefold(),
            item.name.casefold(),
            item.reddlc_path.casefold(),
        ))
        return definitions

    @staticmethod
    def BuildSourceSignature(source_roots=None):
        normalized_sources = DLCManager._normalized_source_roots(source_roots)
        files = []
        source_signatures = []
        for source in normalized_sources:
            if source.get("source_type") == "assets_mods":
                try:
                    from ..Bundles.BundleManager import BundleManager

                    bundle_signature, bundle_source = BundleManager.BuildSourceSignature(True)
                    source_signatures.append(bundle_signature)
                    source["bundle_source"] = bundle_source
                except Exception:
                    log.debug("Failed to build assets mod bundle signature for DLC definitions.", exc_info=True)
                continue
            for dlc_root in _candidate_dlc_roots(source["root_path"], source["rel_paths"]):
                files.extend(_iter_reddlc_files_one_level(dlc_root) or [])
        signature = cache_meta.compute_signature(sorted({_norm_fs_path(path): path for path in files}.values()))
        if source_signatures:
            sha = hashlib.sha1(signature.get("hash", "").encode("ascii", "ignore"))
            for source_signature in source_signatures:
                sha.update(str(source_signature.get("hash", "")).encode("ascii", "ignore"))
                signature["count"] += int(source_signature.get("count", 0) or 0)
                signature["total_size"] += int(source_signature.get("total_size", 0) or 0)
                signature["latest_mtime"] = max(
                    int(signature.get("latest_mtime", 0) or 0),
                    int(source_signature.get("latest_mtime", 0) or 0),
                )
            signature["hash"] = sha.hexdigest()
        source = {
            "type": "dlc_definitions",
            "serialization": DLCManager.SerializationVersion(),
            "source_roots": normalized_sources,
        }
        return signature, source

    def LoadAll(self, source_roots=None):
        self.source_roots = DLCManager._normalized_source_roots(source_roots)
        self.definitions = DLCManager.DiscoverDefinitions(self.source_roots)
        self.MountDLCs()

    def ApplyEnabledMap(self, enabled_by_key=None):
        enabled_by_key = dict(enabled_by_key or {})
        if not enabled_by_key:
            for definition in self.definitions:
                definition.enabled = bool(definition.initially_enabled)
            self.MountDLCs()
            return
        enabled_by_path = {
            _norm_fs_path(key): bool(value)
            for key, value in enabled_by_key.items()
            if "\\" in str(key) or "/" in str(key) or ":" in str(key)
        }
        for definition in self.definitions:
            if definition.key in enabled_by_key:
                definition.enabled = bool(enabled_by_key[definition.key])
                continue
            path_key = _norm_fs_path(definition.reddlc_path)
            if path_key in enabled_by_path:
                definition.enabled = bool(enabled_by_path[path_key])
        self.MountDLCs()

    def GetEnabledDefinitions(self) -> list[DLCDefinition]:
        return [definition for definition in self.definitions if definition.enabled]

    def GetEnabledContent(self) -> list[DLCDefinition]:
        return list(self.mounted_content)

    def GetAppearanceMounterTable(self) -> dict[str, list[dict]]:
        table = {}
        for definition in self.mounted_content:
            _merge_mounter_table(table, getattr(definition, "appearance_mounters", {}) or {})
        _dedupe_appearance_table(table)
        return table

    def GetTemplateParamMounterTable(self) -> dict[str, list[dict]]:
        table = {}
        for definition in self.mounted_content:
            _merge_mounter_table(table, getattr(definition, "template_param_mounters", {}) or {})
        _dedupe_template_param_table(table)
        return table

    def MountDLCs(self):
        self.mounted_content = self.GetEnabledDefinitions()
        log.debug("[DLC] Mounted %d DLC definition(s).", len(self.mounted_content))

    def UnmountDLCs(self):
        count = len(self.mounted_content)
        self.mounted_content = []
        log.debug("[DLC] Unmounted %d DLC definition(s).", count)

    @staticmethod
    def Get(source_roots=None, enabled_by_key=None, reset_cache=False):
        source_roots = DLCManager._normalized_source_roots(source_roots)
        instance_manager = DLCManager.InstanceManager

        if (
            instance_manager is not None
            and getattr(instance_manager, "source_roots", None) == source_roots
            and not reset_cache
        ):
            instance_manager.ApplyEnabledMap(enabled_by_key)
            return instance_manager

        signature, source = DLCManager.BuildSourceSignature(source_roots)
        cache_root = get_cache_root(create=True)
        cache_dir = os.path.join(cache_root, "DLC")
        os.makedirs(cache_dir, exist_ok=True)
        cache_name = "dlc_definition_cache.pkl"
        filename = os.path.join(cache_dir, cache_name)

        start_time = time.time()

        def load_manager():
            manager = DLCManager()
            manager.LoadAll(source_roots)
            with open(filename, "wb") as f:
                pickle.dump(manager, f, protocol=pickle.HIGHEST_PROTOCOL)
            meta = cache_meta.make_meta(cache_name, filename, signature, source)
            cache_meta.save_meta(cache_meta.get_meta_path(filename), meta)
            return manager

        if not os.path.exists(filename) or reset_cache:
            manager = load_manager()
        else:
            meta = cache_meta.load_meta(cache_meta.get_meta_path(filename))
            cached_source = meta.get("source", {}) or {}
            if cached_source.get("serialization") != DLCManager.SerializationVersion():
                manager = load_manager()
            elif not cache_meta.signatures_match(meta.get("signature", {}), signature):
                manager = load_manager()
            else:
                try:
                    with open(filename, "rb") as f:
                        manager = pickle.load(f)
                    if getattr(manager, "source_roots", None) != source_roots:
                        manager = load_manager()
                except Exception:
                    manager = load_manager()

        manager.ApplyEnabledMap(enabled_by_key)
        DLCManager.InstanceManager = manager
        log.info("Loaded DLC Definition Cache in %.2f seconds (%d definitions)", time.time() - start_time, len(manager.definitions))
        return manager
