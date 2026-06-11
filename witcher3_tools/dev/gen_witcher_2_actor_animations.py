"""
One-off generator: scans a Witcher 2 install for .w2anims files, parses each
via the existing CR2W stack, and emits one CSV row per CSkeletalAnimation.

Output schema matches witcher3_tools/CR2W/data/actor_animations.csv exactly so
the Quick Animation Browser can read it through the same code path:

    file;cat1;cat2;cat3;id;caption;frames

Category scheme (per design decision 2026-05-26):
  cat1 = friendlyName from W2's data/globals/animations.csv if the animset is
         listed there (e.g. "work", "combat_sword_man"). Otherwise falls back
         to the first folder segment after `characters\\` (e.g. "monsters",
         "animals", "templates", "interaction"), or "uncategorized".
  cat2 = parent folder of the .w2anims file -- the character/monster identity
         (e.g. "man", "woman", "nekker", "dog").
  cat3 = animset filename without extension (e.g. "triss_combat",
         "monster_animset").

Output is sorted by (cat1, cat2, cat3, id) so re-runs produce stable diffs.

Run as a plain Python script (no Blender required):
    python witcher3_tools/dev/gen_witcher_2_actor_animations.py \
        --game-path "G:/GOG Games/The Witcher 2"

If --game-path is omitted, the script reads witcher2_game_path from
witcher3_tools/dev/dev_config.json -> addon_prefs_defaults.

For incremental validation while developing, use --slice (substring or regex
matched against the depot-relative path) and/or --limit:
    python witcher3_tools/dev/gen_witcher_2_actor_animations.py \
        --slice "triss" --out witcher_2_actor_animations.slice.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


def _install_namespace_stub(qualified_name: str, package_path: Path) -> None:
    if qualified_name in sys.modules:
        return
    module = types.ModuleType(qualified_name)
    module.__path__ = [str(package_path)]
    module.__package__ = qualified_name
    sys.modules[qualified_name] = module


_install_namespace_stub("witcher3_tools", REPO_ROOT / "witcher3_tools")
_install_namespace_stub("witcher3_tools.CR2W", REPO_ROOT / "witcher3_tools" / "CR2W")

from witcher3_tools.CR2W.dc_anims import load_w2_anims_info  # noqa: E402


log = logging.getLogger("gen_w2_anim_csv")


DEFAULT_OUT_REL = Path("witcher3_tools/CR2W/data/witcher_2_actor_animations.csv")
DEV_CONFIG = REPO_ROOT / "witcher3_tools" / "dev" / "dev_config.json"


@dataclass
class AnimRow:
    file: str        # depot-relative backslash path, lowercased
    cat1: str
    cat2: str
    cat3: str
    id: str
    caption: str
    frames: int


def _load_witcher2_game_path_from_dev_config() -> Optional[str]:
    if not DEV_CONFIG.is_file():
        return None
    try:
        data = json.loads(DEV_CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return None
    candidate = (data.get("addon_prefs_defaults") or {}).get("witcher2_game_path")
    candidate = str(candidate or "").strip()
    return candidate or None


def _depot_relative(abs_path: Path, data_root: Path) -> str:
    """Return depot path relative to <game>/data, backslash-style, lowercased.

    Mirrors how W3's actor_animations.csv stores paths.
    """
    rel = abs_path.relative_to(data_root)
    return str(rel).replace("/", "\\").lower()


def _parse_globals_animations_csv(data_root: Path) -> Dict[str, str]:
    """Read W2's data/globals/animations.csv -> {animset_depot_path: friendlyName}.

    Keys are lowercased backslash-style paths so they line up with what we
    derive from os.walk. Comment lines (starting with '#') are skipped.
    """
    path = data_root / "globals" / "animations.csv"
    if not path.is_file():
        log.warning("globals/animations.csv not found at %s; cat1 will be path-derived for all rows.", path)
        return {}

    mapping: Dict[str, str] = {}
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(
            (line for line in f if not line.lstrip().startswith("#")),
            delimiter=";",
        )
        for row in reader:
            friendly = str(row.get("friendlyName") or "").strip()
            depot = str(row.get("animsetDepotPath") or "").strip()
            if not friendly or not depot:
                continue
            mapping[depot.replace("/", "\\").lower()] = friendly
    log.info("Loaded %d entries from globals/animations.csv", len(mapping))
    return mapping


# Generic folder names that don't identify a character or group -- skip when
# looking for cat2 ("the character class") so we surface the meaningful segment.
_GENERIC_FOLDERS = {"animation", "animations", "anims", "dialog", "dialogs", "data"}


def _categorize(depot_path: str, globals_map: Dict[str, str]) -> Tuple[str, str, str]:
    """Derive (cat1, cat2, cat3) for a .w2anims depot path."""
    friendly = globals_map.get(depot_path)

    parts = [p for p in depot_path.split("\\") if p]
    # Typical shapes:
    #   characters\templates\<class>\animation\<set>.w2anims
    #   characters\monsters\<creature>\<set>.w2anims
    #   characters\animals\<animal>\<set>.w2anims
    #   characters\templates\interaction\dialog\<set>.w2anims
    #   cutscenes\...\<set>.w2anims
    #   dlc\<name>\...\<set>.w2anims

    cat3 = Path(parts[-1]).stem if parts else "unknown"

    # cat2: walk back from the parent folder, skipping generic names like
    # "animation"/"dialogs", until we hit something character-identifying.
    cat2 = ""
    for segment in reversed(parts[:-1]):
        if segment.lower() not in _GENERIC_FOLDERS:
            cat2 = segment
            break

    # cat1: friendlyName if matched, else group name after the top folder.
    if friendly:
        cat1 = friendly
    else:
        top = parts[0] if parts else ""
        if top in {"characters", "cutscenes"} and len(parts) >= 2:
            cat1 = parts[1]
        elif top == "dlc" and len(parts) >= 3:
            cat1 = f"dlc-{parts[2]}"
        elif top:
            cat1 = top
        else:
            cat1 = "uncategorized"

    return cat1, cat2, cat3


def _walk_w2anims(data_root: Path, slice_pattern: Optional[re.Pattern], limit: Optional[int]) -> Iterable[Path]:
    count = 0
    for root, _dirs, files in os.walk(data_root):
        for name in files:
            if not name.lower().endswith(".w2anims"):
                continue
            p = Path(root) / name
            if slice_pattern is not None:
                rel = _depot_relative(p, data_root)
                if not slice_pattern.search(rel):
                    continue
            yield p
            count += 1
            if limit is not None and count >= limit:
                return


def _is_w2_cr2w(path: Path) -> bool:
    """Cheap header check -- magic + version <= 115."""
    try:
        with path.open("rb") as f:
            magic = f.read(4)
            if magic != b"CR2W":
                return False
            version_bytes = f.read(4)
            if len(version_bytes) < 4:
                return False
            version = int.from_bytes(version_bytes, "little")
            return version <= 115
    except Exception:
        return False


def _rows_from_animset(abs_path: Path, depot_path: str, globals_map: Dict[str, str]) -> List[AnimRow]:
    """Parse one .w2anims and emit a row per CSkeletalAnimation entry."""
    try:
        anim_set = load_w2_anims_info(str(abs_path))
    except Exception as exc:
        log.error("Failed to parse %s: %s", depot_path, exc)
        return []

    cat1, cat2, cat3 = _categorize(depot_path, globals_map)
    rows: List[AnimRow] = []

    for entry in getattr(anim_set, "animations", []) or []:
        anim = getattr(entry, "animation", None)
        if anim is None:
            continue
        name = str(getattr(anim, "name", "") or "").strip()
        if not name or name == "unknown":
            continue

        duration = float(getattr(anim, "duration", 0.0) or 0.0)
        fps = float(getattr(anim, "framesPerSecond", 30.0) or 30.0)
        if fps <= 0.0:
            fps = 30.0
        frames = max(0, int(round(duration * fps)))

        caption = name.replace("_", " ")

        rows.append(AnimRow(
            file=depot_path,
            cat1=cat1,
            cat2=cat2,
            cat3=cat3,
            id=name,
            caption=caption,
            frames=frames,
        ))
    return rows


def _write_csv(rows: List[AnimRow], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows_sorted = sorted(rows, key=lambda r: (r.cat1, r.cat2, r.cat3, r.id))
    with out_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, delimiter=";", lineterminator="\n")
        writer.writerow(["file", "cat1", "cat2", "cat3", "id", "caption", "frames"])
        for r in rows_sorted:
            writer.writerow([r.file, r.cat1, r.cat2, r.cat3, r.id, r.caption, r.frames])


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1], formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--game-path", help="Path to The Witcher 2 install (the folder containing 'data/'). Falls back to dev_config.json.")
    p.add_argument("--scan-root", default="characters", help="Subfolder under <data>/ to scan (default: characters). Use '.' to scan all of <data>/.")
    p.add_argument("--out", help=f"Output CSV path (default: <repo>/{DEFAULT_OUT_REL}).")
    p.add_argument("--slice", help="Substring or regex applied to depot-relative paths; only matching files are processed.")
    p.add_argument("--limit", type=int, help="Stop after processing this many files (for quick validation).")
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose logging.")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_argparser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    game_path = args.game_path or _load_witcher2_game_path_from_dev_config()
    if not game_path:
        log.error("No --game-path given and dev_config.json has no addon_prefs_defaults.witcher2_game_path.")
        return 2
    data_root = Path(game_path) / "data"
    if not data_root.is_dir():
        log.error("Not a directory: %s (expected <game>/data)", data_root)
        return 2

    scan_root = data_root / args.scan_root if args.scan_root not in {".", ""} else data_root
    if not scan_root.is_dir():
        log.error("Scan root does not exist: %s", scan_root)
        return 2

    out_path = Path(args.out).resolve() if args.out else (REPO_ROOT / DEFAULT_OUT_REL)
    slice_pattern = re.compile(args.slice, re.IGNORECASE) if args.slice else None

    log.info("Game data root: %s", data_root)
    log.info("Scanning under: %s", scan_root)
    log.info("Output CSV:     %s", out_path)
    if slice_pattern:
        log.info("Slice pattern:  %s", slice_pattern.pattern)
    if args.limit:
        log.info("File limit:     %d", args.limit)

    globals_map = _parse_globals_animations_csv(data_root)

    started = time.monotonic()
    all_rows: List[AnimRow] = []
    files_seen = 0
    files_parsed = 0
    files_skipped_not_w2 = 0
    files_failed = 0

    for abs_path in _walk_w2anims(scan_root, slice_pattern, args.limit):
        files_seen += 1
        depot_path = _depot_relative(abs_path, data_root)
        if not _is_w2_cr2w(abs_path):
            files_skipped_not_w2 += 1
            log.debug("Skip (not W2 CR2W): %s", depot_path)
            continue
        rows = _rows_from_animset(abs_path, depot_path, globals_map)
        if not rows:
            files_failed += 1
            log.warning("No animations extracted from %s (parse errors above)", depot_path)
            continue
        files_parsed += 1
        all_rows.extend(rows)
        log.debug("Parsed %3d anims from %s", len(rows), depot_path)

    _write_csv(all_rows, out_path)
    elapsed = time.monotonic() - started
    log.info(
        "Done in %.1fs. files_seen=%d parsed=%d skipped_not_w2=%d failed=%d rows=%d",
        elapsed, files_seen, files_parsed, files_skipped_not_w2, files_failed, len(all_rows),
    )
    log.info("Wrote: %s", out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
