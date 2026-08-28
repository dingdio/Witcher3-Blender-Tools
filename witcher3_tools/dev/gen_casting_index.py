from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "witcher3_tools" / "CR2W" / "data"
DEFAULT_OUTPUT = DATA_DIR / "casting_index.json"

CURATED_ALIASES = {
    "player": "geralt",
    "ciri": "cirilla",
    "yen": "yennefer",
    "jaskier": "dandelion",
    "duchess": "anna henrietta",
    "dettlaff": "dettlaff van eretein",
    "master mirror": "gaunter odimm",
    "odim": "gaunter odimm",
    "gaunter o dimm": "gaunter odimm",
    "bloody baron": "baron",
    "roach": "player horse",
}

_DISPLAY_NAME_RE = re.compile(r"^\s*displayName:\s*(.+?)\s*(?:#.*)?$")
_VOICE_TAG_RE = re.compile(r"^\s*voiceTag:\s*(.+?)\s*(?:#.*)?$")
_SKELETON_RE = re.compile(r"^\s*skeleton:\s*(\S+\.w2rig)\s*$")
_APPEARANCES_RE = re.compile(r"^(\s*)appearances:\s*#")
_USED_APPEARANCES_RE = re.compile(r"^(\s*)usedAppearances:\s*#")
_ANIMSETS_RE = re.compile(r"^(\s*)animationSets:\s*#")
_NAME_COMMENT_RE = re.compile(r"^\s*## name:\s*(.+?)\s*$")
_LIST_ITEM_RE = re.compile(r"^\s*-\s*(.+?)\s*$")


def _clean(value: str) -> str:
    value = str(value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return value.strip()


def scan_template_yml(path: Path) -> dict:
    info = {"displayName": "", "voicetags": [], "appearances": [],
            "usedAppearances": [], "rigs": [], "animsets": []}
    block = None
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            if block is not None:
                indent = len(line) - len(line.lstrip())
                kind, block_indent = block
                if indent <= block_indent and not stripped.startswith("#"):
                    block = None
                else:
                    if kind == "appearances":
                        m = _NAME_COMMENT_RE.match(line)
                        if m:
                            name = _clean(m.group(1))
                            if name and name not in info["appearances"]:
                                info["appearances"].append(name)
                    elif kind in ("used", "animsets"):
                        m = _LIST_ITEM_RE.match(line)
                        if m and not stripped.startswith("- \"."):
                            value = _clean(m.group(1))
                            key = "usedAppearances" if kind == "used" else "animsets"
                            if value and value not in info[key]:
                                info[key].append(value)
                    continue

            m = _APPEARANCES_RE.match(line)
            if m:
                block = ("appearances", len(m.group(1)))
                continue
            m = _USED_APPEARANCES_RE.match(line)
            if m:
                block = ("used", len(m.group(1)))
                continue
            m = _ANIMSETS_RE.match(line)
            if m:
                block = ("animsets", len(m.group(1)))
                continue
            m = _DISPLAY_NAME_RE.match(line)
            if m and not info["displayName"]:
                info["displayName"] = _clean(m.group(1))
                continue
            m = _VOICE_TAG_RE.match(line)
            if m:
                tag = _clean(m.group(1)).upper()
                if tag and tag not in info["voicetags"]:
                    info["voicetags"].append(tag)
                continue
            m = _SKELETON_RE.match(line)
            if m:
                rig = _clean(m.group(1))
                if rig and rig not in info["rigs"]:
                    info["rigs"].append(rig)
    return info


def _norm_alias(value: str) -> str:
    value = re.sub(r"[_\-.]+", " ", str(value or "").lower())
    value = re.sub(r"[^a-z0-9 ]+", "", value)
    return re.sub(r"\s+", " ", value).strip()


def _journal_alias(journal_path: str) -> str:
    base = journal_path.replace("\\", "/").rsplit("/", 1)[-1]
    base = base.rsplit(".", 1)[0].strip()
    return _norm_alias(base)


def build_index(corpus_root: Path) -> dict:
    templates: dict[str, dict] = {}

    def entry(path: str) -> dict:
        path = path.replace("/", "\\")
        return templates.setdefault(path, {
            "caption": "", "category": "", "aliases": [],
            "displayName": "", "voicetags": [], "appearances": [],
            "usedAppearances": [], "rigs": [], "animsets": [],
        })

    def add_alias(rec: dict, value: str):
        alias = _norm_alias(value)
        if alias and alias not in rec["aliases"]:
            rec["aliases"].append(alias)

    csv_path = DATA_DIR / "actor_templates.csv"
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.reader(handle, delimiter=";"):
            if len(row) < 6 or row[4] in ("ID", ""):
                continue
            _, cat1, cat2, _cat3, path, caption = row[:6]
            rec = entry(path)
            rec["caption"] = caption
            add_alias(rec, caption)
            cat2 = (cat2 or "").lower()
            if cat1 == "player":
                rec["category"] = "player"
            elif "animal" in cat2:
                rec["category"] = "animal"
            elif "monster" in cat2:
                rec["category"] = "monster"

    for fname, category in (
        ("journal_entity_overrides.characters.json", "character"),
        ("journal_entity_overrides.bestiary.json", "monster"),
    ):
        data = json.loads((DATA_DIR / fname).read_text(encoding="utf-8"))
        for journal, template in data.items():
            if not template:
                continue
            rec = entry(template)
            rec["category"] = rec["category"] or category
            rec["canonical"] = True
            add_alias(rec, _journal_alias(journal))

    scanned = missing = 0
    for path, rec in templates.items():
        yml = corpus_root / (path.replace("\\", "/") + ".yml")
        if not yml.is_file():
            missing += 1
            continue
        scanned += 1
        info = scan_template_yml(yml)
        rec.update({k: info[k] for k in
                    ("displayName", "voicetags", "appearances", "usedAppearances", "rigs", "animsets")})
        if info["displayName"]:
            add_alias(rec, info["displayName"])
        base = path.rsplit("\\", 1)[-1].rsplit(".", 1)[0]
        add_alias(rec, base)
        family = re.sub(r"(_+(lvl|level)\d+|_+\d+|__+.*)$", "", base)
        if family and family != base:
            add_alias(rec, family)

    for path, rec in templates.items():
        if not rec["category"]:
            lowered = path.lower()
            if "main_npc" in lowered or "\\quests\\" in lowered and "npc" in lowered:
                rec["category"] = "character"
            elif "monster" in lowered or "enemy_templates" in lowered:
                rec["category"] = "monster"
            elif "\\animals\\" in lowered:
                rec["category"] = "animal"
            else:
                rec["category"] = "background"

    aliases: dict[str, list] = {}
    for path, rec in templates.items():
        for alias in rec["aliases"]:
            aliases.setdefault(alias, []).append(path)

    def rank(path):
        rec = templates[path]
        return (0 if rec.get("canonical") else 1,
                0 if rec["category"] in ("character", "player") else 1,
                len(path))
    for alias, paths in aliases.items():
        paths.sort(key=rank)

    unresolved = []
    for alias, target in CURATED_ALIASES.items():
        target_norm = _norm_alias(target)
        if target_norm in aliases:
            aliases[_norm_alias(alias)] = list(aliases[target_norm])
        else:
            unresolved.append((alias, target))

    strings: list[str] = []
    string_ids: dict[str, int] = {}

    def intern(value: str) -> int:
        idx = string_ids.get(value)
        if idx is None:
            idx = string_ids[value] = len(strings)
            strings.append(value)
        return idx

    for rec in templates.values():
        rec["animsets"] = [intern(p) for p in rec["animsets"]]
        rec["rigs"] = [intern(p) for p in rec["rigs"]]

    print(f"templates: {len(templates)}  scanned: {scanned}  no-corpus-dump: {missing}  "
          f"aliases: {len(aliases)}  interned-paths: {len(strings)}")
    for alias, target in unresolved:
        print(f"  WARNING curated alias '{alias}' -> '{target}' has no auto-generated target")

    return {"version": 1, "strings": strings, "templates": templates, "aliases": aliases}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    index = build_index(args.corpus)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(index, handle, separators=(",", ":"))
    print(f"wrote {args.output} ({args.output.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
