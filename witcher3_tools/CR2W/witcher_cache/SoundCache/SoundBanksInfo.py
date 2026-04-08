import argparse
import gzip
import json
import logging
import os
import xml.etree.ElementTree as ET
from typing import Dict, Optional


log = logging.getLogger(__name__)


def _normalize_sound_path(path: str) -> str:
    if not path:
        return ""
    return str(path).replace("/", "\\").strip().strip("\\")


def _local_tag_name(elem) -> str:
    tag = getattr(elem, "tag", "") or ""
    return tag.rsplit("}", 1)[-1]


def _iter_children_named(elem, child_name: str):
    if elem is None:
        return
    for child in list(elem):
        if _local_tag_name(child) == child_name:
            yield child


def _find_child_named(elem, child_name: str):
    for child in _iter_children_named(elem, child_name):
        return child
    return None


def _find_child_text(elem, child_name: str, default: str = "") -> str:
    child = _find_child_named(elem, child_name)
    if child is None or child.text is None:
        return default
    return child.text


class SoundBanksInfoXML:
    """Lookup loaded from WolvenKit XML or a compact derived JSON file."""

    COMPACT_FORMAT_VERSION = 2

    def __init__(self, filename: str):
        self.filename = filename
        self.PlatForm = ""
        self.SchemaVersion = ""
        self.RootPaths: Dict[str, str] = {}
        self.StreamedFilesById: Dict[str, Dict[str, str]] = {}
        self.BanksById: Dict[str, Dict[str, str]] = {}
        self.EventsByName: Dict[str, Dict[str, object]] = {}
        self.EventsById: Dict[str, Dict[str, object]] = {}
        self._EventNameCasefold: Dict[str, str] = {}
        self._loaded = False
        self._load()

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True

        if not self.filename or not os.path.exists(self.filename):
            log.warning("Sound banks metadata file not found: %s", self.filename)
            return

        try:
            if self.filename.lower().endswith((".json", ".json.gz", ".gz")):
                self._load_compact()
            else:
                self._load_xml()
        except Exception:
            log.warning("Failed to open sound banks metadata file: %s", self.filename, exc_info=True)
            return

    @staticmethod
    def _open_text(path: str, mode: str):
        if path.lower().endswith(".gz"):
            return gzip.open(path, mode, encoding="utf-8")
        return open(path, mode, encoding="utf-8")

    @staticmethod
    def _normalize_entry(entry_id: str, data: Optional[Dict[str, str]]) -> Optional[Dict[str, str]]:
        normalized_id = str(entry_id or "").strip()
        if not normalized_id:
            return None
        values = data if isinstance(data, dict) else {}
        return {
            "id": normalized_id,
            "language": str(values.get("language") or values.get("Language") or "").strip(),
            "short_name": _normalize_sound_path(values.get("short_name") or values.get("ShortName") or ""),
            "path": _normalize_sound_path(values.get("path") or values.get("Path") or ""),
            "streamed_file_ids": SoundBanksInfoXML._normalize_id_list(
                values.get("streamed_file_ids") or values.get("StreamedFileIds") or ()
            ),
            "included_memory_file_ids": SoundBanksInfoXML._normalize_id_list(
                values.get("included_memory_file_ids") or values.get("IncludedMemoryFileIds") or ()
            ),
            "excluded_memory_file_ids": SoundBanksInfoXML._normalize_id_list(
                values.get("excluded_memory_file_ids") or values.get("ExcludedMemoryFileIds") or ()
            ),
        }

    @staticmethod
    def _normalize_id_list(values) -> list[str]:
        normalized = []
        seen = set()
        for value in values or ():
            text = str(value or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            normalized.append(text)
        return normalized

    @staticmethod
    def _normalize_event_entry(event_name: str, data: Optional[Dict[str, object]]) -> Optional[Dict[str, object]]:
        values = data if isinstance(data, dict) else {}
        normalized_name = str(
            event_name or values.get("name") or values.get("Name") or ""
        ).strip()
        if not normalized_name:
            return None
        return {
            "id": str(values.get("id") or values.get("Id") or "").strip(),
            "name": normalized_name,
            "object_path": _normalize_sound_path(values.get("object_path") or values.get("ObjectPath") or ""),
            "bank_id": str(values.get("bank_id") or values.get("BankId") or "").strip(),
            "bank_path": _normalize_sound_path(values.get("bank_path") or values.get("BankPath") or ""),
            "streamed_file_ids": SoundBanksInfoXML._normalize_id_list(
                values.get("streamed_file_ids") or values.get("StreamedFileIds") or ()
            ),
            "included_memory_file_ids": SoundBanksInfoXML._normalize_id_list(
                values.get("included_memory_file_ids") or values.get("IncludedMemoryFileIds") or ()
            ),
            "excluded_memory_file_ids": SoundBanksInfoXML._normalize_id_list(
                values.get("excluded_memory_file_ids") or values.get("ExcludedMemoryFileIds") or ()
            ),
        }

    def _rebuild_indexes(self) -> None:
        self.EventsById = {}
        self._EventNameCasefold = {}
        for event_name, entry in self.EventsByName.items():
            normalized_name = str(event_name or "").strip()
            if normalized_name:
                self._EventNameCasefold.setdefault(normalized_name.lower(), normalized_name)
            event_id = str((entry or {}).get("id") or "").strip()
            if event_id and event_id not in self.EventsById:
                self.EventsById[event_id] = entry

    def _merge_streamed_file_entry(self, file_id: str, data: Optional[Dict[str, str]]) -> None:
        normalized = self._normalize_entry(file_id, data)
        if normalized is None:
            return
        existing = self.StreamedFilesById.get(normalized["id"], {})
        merged = dict(existing) if isinstance(existing, dict) else {}
        for key, value in normalized.items():
            if isinstance(value, list):
                if value:
                    merged[key] = self._normalize_id_list((merged.get(key) or []) + value)
            elif value:
                merged[key] = value
        if "id" not in merged:
            merged["id"] = normalized["id"]
        self.StreamedFilesById[normalized["id"]] = merged

    def _collect_file_ids(self, parent_elem, container_name: str) -> list[str]:
        file_ids = []
        seen = set()
        container = _find_child_named(parent_elem, container_name)
        if container is None:
            return file_ids

        for file_elem in _iter_children_named(container, "File"):
            file_id = str(file_elem.get("Id") or "").strip()
            if not file_id:
                continue
            self._merge_streamed_file_entry(
                file_id,
                {
                    "language": (file_elem.get("Language") or "").strip(),
                    "short_name": _find_child_text(file_elem, "ShortName", default=""),
                    "path": _find_child_text(file_elem, "Path", default=""),
                },
            )
            if file_id not in seen:
                seen.add(file_id)
                file_ids.append(file_id)
        return file_ids

    def _load_compact(self) -> None:
        with self._open_text(self.filename, "rt") as handle:
            data = json.load(handle)

        self.PlatForm = str(data.get("platform") or data.get("PlatForm") or "").strip()
        self.SchemaVersion = str(data.get("schema_version") or data.get("SchemaVersion") or "").strip()

        root_paths = data.get("root_paths") or data.get("RootPaths") or {}
        if isinstance(root_paths, dict):
            self.RootPaths = {
                str(key): str(value).strip()
                for key, value in root_paths.items()
                if str(value).strip()
            }

        streamed_files = data.get("streamed_files_by_id") or data.get("StreamedFilesById") or {}
        if isinstance(streamed_files, dict):
            for file_id, metadata in streamed_files.items():
                normalized = self._normalize_entry(file_id, metadata)
                if normalized is not None:
                    self.StreamedFilesById[normalized["id"]] = normalized

        banks = data.get("banks_by_id") or data.get("BanksById") or {}
        if isinstance(banks, dict):
            for bank_id, metadata in banks.items():
                normalized = self._normalize_entry(bank_id, metadata)
                if normalized is not None:
                    self.BanksById[normalized["id"]] = normalized

        events = data.get("events_by_name") or data.get("EventsByName") or {}
        if isinstance(events, dict):
            for event_name, metadata in events.items():
                normalized = self._normalize_event_entry(event_name, metadata)
                if normalized is not None:
                    self.EventsByName[normalized["name"]] = normalized

        self._rebuild_indexes()

    def _load_xml(self) -> None:
        context = ET.iterparse(self.filename, events=("start", "end"))

        in_root_paths = False
        in_streamed_files = False
        in_sound_banks = False
        for event, elem in context:
            tag = elem.tag.rsplit("}", 1)[-1]

            if event == "start":
                if tag == "RootPaths":
                    in_root_paths = True
                elif tag == "StreamedFiles":
                    in_streamed_files = True
                elif tag == "SoundBanks":
                    in_sound_banks = True
                continue

            if tag == "SoundBanksInfo":
                self.PlatForm = (elem.get("Platform") or "").strip()
                self.SchemaVersion = (elem.get("SchemaVersion") or "").strip()
            elif in_root_paths and tag not in {"RootPaths", "SoundBanksInfo"}:
                value = (elem.text or "").strip()
                if value:
                    self.RootPaths[tag] = value
            elif in_streamed_files and tag == "File":
                file_id = (elem.get("Id") or "").strip()
                if file_id:
                    self._merge_streamed_file_entry(
                        file_id,
                        {
                            "language": (elem.get("Language") or "").strip(),
                            "short_name": _find_child_text(elem, "ShortName", default=""),
                            "path": _find_child_text(elem, "Path", default=""),
                        },
                    )
                elem.clear()
            elif in_sound_banks and tag == "SoundBank":
                bank_id = (elem.get("Id") or "").strip()
                if bank_id:
                    bank_entry = {
                        "id": bank_id,
                        "language": (elem.get("Language") or "").strip(),
                        "short_name": _normalize_sound_path(_find_child_text(elem, "ShortName", default="")),
                        "path": _normalize_sound_path(_find_child_text(elem, "Path", default="")),
                        "streamed_file_ids": self._collect_file_ids(elem, "ReferencedStreamedFiles"),
                        "included_memory_file_ids": self._collect_file_ids(elem, "IncludedMemoryFiles"),
                        "excluded_memory_file_ids": self._collect_file_ids(elem, "ExcludedMemoryFiles"),
                    }
                    self.BanksById[bank_id] = bank_entry

                    included_events = _find_child_named(elem, "IncludedEvents")
                    if included_events is not None:
                        for event_elem in _iter_children_named(included_events, "Event"):
                            event_name = str(event_elem.get("Name") or "").strip()
                            normalized_event = self._normalize_event_entry(
                                event_name,
                                {
                                    "id": event_elem.get("Id"),
                                    "name": event_name,
                                    "object_path": event_elem.get("ObjectPath"),
                                    "bank_id": bank_id,
                                    "bank_path": bank_entry.get("path", ""),
                                    "streamed_file_ids": self._collect_file_ids(event_elem, "ReferencedStreamedFiles"),
                                    "included_memory_file_ids": self._collect_file_ids(event_elem, "IncludedMemoryFiles"),
                                    "excluded_memory_file_ids": self._collect_file_ids(event_elem, "ExcludedMemoryFiles"),
                                },
                            )
                            if normalized_event is not None:
                                self.EventsByName[normalized_event["name"]] = normalized_event
                elem.clear()
            elif tag == "RootPaths":
                in_root_paths = False
                elem.clear()
            elif tag == "StreamedFiles":
                in_streamed_files = False
                elem.clear()
            elif tag == "SoundBanks":
                in_sound_banks = False
                elem.clear()

        self._rebuild_indexes()

    def to_compact_dict(self) -> Dict[str, object]:
        return {
            "format_version": self.COMPACT_FORMAT_VERSION,
            "platform": self.PlatForm,
            "schema_version": self.SchemaVersion,
            "root_paths": dict(self.RootPaths),
            "streamed_files_by_id": dict(self.StreamedFilesById),
            "banks_by_id": dict(self.BanksById),
            "events_by_name": dict(self.EventsByName),
        }

    def save_compact(self, filename: str) -> None:
        payload = self.to_compact_dict()
        output_dir = os.path.dirname(filename)
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
        with self._open_text(filename, "wt") as handle:
            json.dump(payload, handle, separators=(",", ":"), ensure_ascii=True)

    def lookup(self, archive_name: str) -> Optional[Dict[str, str]]:
        normalized = _normalize_sound_path(archive_name)
        if not normalized:
            return None
        stem, ext = os.path.splitext(os.path.basename(normalized))
        if not stem:
            return None
        ext = ext.lower()
        if ext == ".wem":
            return self.StreamedFilesById.get(stem)
        if ext == ".bnk":
            return self.BanksById.get(stem)
        return None

    def resolve_archive_name(self, archive_name: str) -> str:
        normalized = _normalize_sound_path(archive_name)
        metadata = self.lookup(normalized)
        if not metadata:
            return normalized
        return _normalize_sound_path(metadata.get("path") or normalized)

    def lookup_event(self, event_name: str) -> Optional[Dict[str, object]]:
        normalized_name = str(event_name or "").strip()
        if not normalized_name:
            return None
        entry = self.EventsByName.get(normalized_name)
        if entry is not None:
            return entry
        canonical_name = self._EventNameCasefold.get(normalized_name.lower())
        if canonical_name:
            return self.EventsByName.get(canonical_name)
        return None

    def resolve_event_name(self, event_name: str) -> list[str]:
        event_entry = self.lookup_event(event_name)
        if not event_entry:
            return []

        candidate_file_ids = []
        for key in ("streamed_file_ids", "included_memory_file_ids", "excluded_memory_file_ids"):
            candidate_file_ids.extend(event_entry.get(key) or [])

        bank_entry = self.BanksById.get(str(event_entry.get("bank_id") or "").strip(), {})
        if isinstance(bank_entry, dict):
            for key in ("streamed_file_ids", "included_memory_file_ids", "excluded_memory_file_ids"):
                candidate_file_ids.extend(bank_entry.get(key) or [])

        resolved_paths = []
        seen = set()
        for file_id in self._normalize_id_list(candidate_file_ids):
            metadata = self.StreamedFilesById.get(file_id) or {}
            path = _normalize_sound_path(metadata.get("path") or "")
            if path and path not in seen:
                seen.add(path)
                resolved_paths.append(path)

        if resolved_paths:
            return resolved_paths

        bank_path = _normalize_sound_path(
            event_entry.get("bank_path") or (bank_entry.get("path") if isinstance(bank_entry, dict) else "")
        )
        return [bank_path] if bank_path else []


def _default_metadata_path(filename: str) -> str:
    data_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "data"))
    return os.path.join(data_dir, filename)


def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build compact soundbanks metadata for shipping.")
    parser.add_argument(
        "--input",
        dest="input_path",
        default=_default_metadata_path("soundbanksinfo.xml"),
        help="Path to source soundbanksinfo XML or compact JSON file.",
    )
    parser.add_argument(
        "--output",
        dest="output_path",
        default=_default_metadata_path("soundbanksinfo.json.gz"),
        help="Path to compact JSON(.gz) output file.",
    )
    args = parser.parse_args(argv)

    info = SoundBanksInfoXML(args.input_path)
    info.save_compact(args.output_path)

    input_size = os.path.getsize(args.input_path) if os.path.exists(args.input_path) else 0
    output_size = os.path.getsize(args.output_path) if os.path.exists(args.output_path) else 0
    print(f"Wrote {args.output_path} ({output_size} bytes) from {args.input_path} ({input_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
