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


class SoundBanksInfoXML:
    """Lookup loaded from WolvenKit XML or a compact derived JSON file."""

    COMPACT_FORMAT_VERSION = 1

    def __init__(self, filename: str):
        self.filename = filename
        self.PlatForm = ""
        self.SchemaVersion = ""
        self.RootPaths: Dict[str, str] = {}
        self.StreamedFilesById: Dict[str, Dict[str, str]] = {}
        self.BanksById: Dict[str, Dict[str, str]] = {}
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
        }

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
                    self.StreamedFilesById[file_id] = {
                        "id": file_id,
                        "language": (elem.get("Language") or "").strip(),
                        "short_name": _normalize_sound_path(elem.findtext("ShortName", default="")),
                        "path": _normalize_sound_path(elem.findtext("Path", default="")),
                    }
                elem.clear()
            elif in_sound_banks and tag == "SoundBank":
                bank_id = (elem.get("Id") or "").strip()
                if bank_id:
                    self.BanksById[bank_id] = {
                        "id": bank_id,
                        "language": (elem.get("Language") or "").strip(),
                        "short_name": _normalize_sound_path(elem.findtext("ShortName", default="")),
                        "path": _normalize_sound_path(elem.findtext("Path", default="")),
                    }
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

    def to_compact_dict(self) -> Dict[str, object]:
        return {
            "format_version": self.COMPACT_FORMAT_VERSION,
            "platform": self.PlatForm,
            "schema_version": self.SchemaVersion,
            "root_paths": dict(self.RootPaths),
            "streamed_files_by_id": dict(self.StreamedFilesById),
            "banks_by_id": dict(self.BanksById),
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
