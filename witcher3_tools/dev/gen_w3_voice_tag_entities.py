from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "witcher3_tools" / "CR2W" / "data"
DEFAULT_OUTPUT = DATA_DIR / "dialogue" / "w3" / "voice_tag_entities.json"

_VOICETAG_BLOCK_RE = re.compile(r"^(\s*)voicetagAppearances\s*:")
_VOICE_TAG_RE = re.compile(r"^\s*voicetag\s*:\s*(.+?)\s*(?:#.*)?$")
_APPEARANCE_RE = re.compile(r"^\s*appearance\s*:\s*(.+?)\s*(?:#.*)?$")


def _clean_scalar(value: str) -> str:
    value = str(value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return value.strip()


def _repo_path_from_yml(path: Path, root: Path) -> str:
    rel = path.relative_to(root).as_posix()
    if rel.lower().endswith(".yml"):
        rel = rel[:-4]
    if rel.lower().endswith(".yaml"):
        rel = rel[:-5]
    return rel.replace("/", "\\")


def _iter_voicetag_appearance_pairs(lines):
    in_block = False
    block_indent = 0
    current_tag = ""
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if not in_block:
            match = _VOICETAG_BLOCK_RE.match(line)
            if not match:
                continue
            in_block = True
            block_indent = len(match.group(1))
            current_tag = ""
            continue

        indent = len(line) - len(line.lstrip())
        if indent <= block_indent and not stripped.startswith("-"):
            in_block = False
            current_tag = ""
            match = _VOICETAG_BLOCK_RE.match(line)
            if match:
                in_block = True
                block_indent = len(match.group(1))
            continue

        tag_match = _VOICE_TAG_RE.match(line)
        if tag_match:
            current_tag = _clean_scalar(tag_match.group(1)).upper()
            continue

        appearance_match = _APPEARANCE_RE.match(line)
        if appearance_match and current_tag:
            appearance = _clean_scalar(appearance_match.group(1))
            if appearance:
                yield current_tag, appearance
            current_tag = ""


def build_index(yml_root: Path) -> dict:
    yml_root = Path(yml_root)
    by_tag = {}
    file_count = 0
    link_count = 0

    for path in sorted(yml_root.rglob("*.w2ent.yml")):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "voicetagAppearances" not in text:
            continue

        file_count += 1
        repo_path = _repo_path_from_yml(path, yml_root)
        for tag, appearance in _iter_voicetag_appearance_pairs(text.splitlines()):
            entries = by_tag.setdefault(tag, {})
            key = (repo_path.lower(), appearance.lower())
            if key in entries:
                continue
            entries[key] = {
                "path": repo_path,
                "appearance": appearance,
            }
            link_count += 1

    voice_tags = {
        tag: sorted(entries.values(), key=lambda item: (item["path"].lower(), item["appearance"].lower()))
        for tag, entries in sorted(by_tag.items())
    }
    return {
        "version": 1,
        "source": "w2ent_yml",
        "file_count": file_count,
        "voice_tag_count": len(voice_tags),
        "link_count": link_count,
        "voice_tags": voice_tags,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build W3 voiceTag -> entity appearance lookup from w2ent YML dumps.")
    parser.add_argument("yml_root", type=Path)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    data = build_index(args.yml_root)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(
        f"Wrote {args.out} "
        f"({args.out.stat().st_size:,} bytes, "
        f"{data['voice_tag_count']} voice tags, {data['link_count']} links)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
