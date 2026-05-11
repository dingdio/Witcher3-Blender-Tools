import logging
log = logging.getLogger(__name__)

from .common_blender import repo_file

_body_anim_csv_cache = None
_mimics_csv_cache = None
DEFAULT_BODY_STATUS = "High"
DEFAULT_BODY_EMOTIONAL_STATE = "Determined"
DEFAULT_BODY_POSE_NAME = "Standing"


def _parse_body_anim_csv():
    """Parse scene_body_animations.csv via repo_file (searches all configured uncook paths).
    Returns {(status_lower, emotional_lower, pose_lower): {"idles": [...], "pose_display": str,
    "status_display": str, "emotional_display": str}}.
    """
    global _body_anim_csv_cache
    if _body_anim_csv_cache is not None:
        return _body_anim_csv_cache
    import csv as _csv
    csv_path = repo_file("gameplay\\globals\\scene_body_animations.csv")
    result = {}
    try:
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            reader = _csv.reader(f, delimiter=";")
            next(reader, None)
            cur_status = cur_emotional = cur_pose = ""
            for row in reader:
                if len(row) < 6:
                    continue
                if row[0].strip():
                    cur_status = row[0].strip()
                if row[1].strip():
                    cur_emotional = row[1].strip()
                if row[2].strip():
                    cur_pose = row[2].strip()
                anim_name = row[4].strip()
                anim_type = row[5].strip()
                if not anim_name:
                    continue
                key = (cur_status.lower(), cur_emotional.lower(), cur_pose.lower())
                entry = result.setdefault(key, {"idles": [], "pose_display": cur_pose,
                                                "status_display": cur_status,
                                                "emotional_display": cur_emotional})
                if anim_type == "Idle":
                    entry["idles"].append(anim_name)
    except Exception:
        log.warning("Failed to parse scene_body_animations.csv (path: %s)", csv_path, exc_info=True)
        result = {}
    _body_anim_csv_cache = result
    return result


def _lookup_dialogset_body_anim(status, emotional_state, pose_name):
    """Return first Idle animation name for (status, emotional_state, pose_name), or None."""
    data = _parse_body_anim_csv()
    if not data:
        return None
    key = (str(status or DEFAULT_BODY_STATUS).strip().lower(),
           str(emotional_state or DEFAULT_BODY_EMOTIONAL_STATE).strip().lower(),
           str(pose_name or DEFAULT_BODY_POSE_NAME).strip().lower())
    idles = (data.get(key) or {}).get("idles") or []
    return idles[0] if idles else None


def _resolve_mimic_layer_anim_candidates(layer_value, layer_column):
    """Return an ordered list of catalog id candidates to try for a face-layer animation.

    Returns [] for None/Zero/empty.
    """
    v = str(layer_value or "").strip()
    if not v or v.upper() in ("NONE", "ZERO"):
        return []

    state = _parse_mimics_csv().get(v.lower())
    raw = (state.get(layer_column) if state else "") or v
    base = raw.strip().replace(" ", "_").lower()

    seen = set()
    candidates = []

    def _push(name):
        n = (name or "").strip()
        if n and n not in seen:
            seen.add(n)
            candidates.append(n)

    # Primary: snake_case + _face
    _push(base if base.endswith("_face") else base + "_face")
    # Abbreviation: 'animation' → 'anim'
    if "_animation" in base:
        abbrev = base.replace("_animation", "_anim")
        _push(abbrev if abbrev.endswith("_face") else abbrev + "_face")
    # State-name fallback (catches CSV/catalog mismatches like Happy → happy_pose_face
    # when the CSV says 'happiness pose'). Layer column 'animation' → 'anim' suffix.
    if state:
        state_token = state["display"].strip().replace(" ", "_").lower()
        col_token = "anim" if layer_column == "animation" else layer_column
        _push(f"{state_token}_{col_token}_face")
    # Last-resort variants without _face / spaces
    _push(base)
    _push(raw.strip())
    return candidates


def _resolve_mimic_layer_anim(layer_value, layer_column):
    """Single-string convenience wrapper (returns the first candidate for UI display)."""
    cands = _resolve_mimic_layer_anim_candidates(layer_value, layer_column)
    return cands[0] if cands else ""


def _parse_mimics_csv():
    """Parse scene_mimics_emotional_states.csv via repo_file.
    Returns {state_lower: {"display": str, "eyes": str, "pose": str, "animation": str}}.
    """
    global _mimics_csv_cache
    if _mimics_csv_cache is not None:
        return _mimics_csv_cache
    import csv as _csv
    csv_path = repo_file("gameplay\\globals\\scene_mimics_emotional_states.csv")
    result = {}
    try:
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            reader = _csv.reader(f, delimiter=";")
            next(reader, None)
            for row in reader:
                if len(row) < 4:
                    continue
                state = row[0].strip()
                if not state or state.upper() == "NO ANIMATION":
                    continue
                result[state.lower()] = {
                    "display": state,
                    "eyes": row[1].strip(),
                    "pose": row[2].strip(),
                    "animation": row[3].strip(),
                }
    except Exception:
        log.warning("Failed to parse scene_mimics_emotional_states.csv (path: %s)", csv_path, exc_info=True)
    _mimics_csv_cache = result
    return result
