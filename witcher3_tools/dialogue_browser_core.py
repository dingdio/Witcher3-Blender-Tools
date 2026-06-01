"""Shared list/filter helpers for the strings and dialogue browsers."""

from __future__ import annotations

import math
import re


def get_item_value(item, key, default=""):
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def clamp_page_size(value, default, min_size, max_size=None):
    try:
        size = int(value)
    except Exception:
        size = int(default)
    size = max(int(min_size), size)
    if max_size is not None:
        size = min(int(max_size), size)
    return size


def page_count(item_count, page_size):
    try:
        count = int(item_count)
    except Exception:
        count = 0
    try:
        size = int(page_size)
    except Exception:
        size = 1
    size = max(1, size)
    return max(1, int(math.ceil(count / size))) if count else 1


def clamp_page_index(page_index, total_pages):
    try:
        index = int(page_index)
    except Exception:
        index = 0
    try:
        pages = int(total_pages)
    except Exception:
        pages = 1
    return max(0, min(index, max(1, pages) - 1))


def page_number_from_index(page_index, total_pages):
    return clamp_page_index(page_index, total_pages) + 1


def page_index_from_number(page_number, total_pages):
    try:
        number = int(page_number)
    except Exception:
        number = 1
    return clamp_page_index(max(1, number) - 1, total_pages)


def page_bounds(item_count, page_index, page_size):
    try:
        count = int(item_count)
    except Exception:
        count = 0
    try:
        size = int(page_size)
    except Exception:
        size = 1
    size = max(1, size)
    pages = page_count(count, size)
    index = clamp_page_index(page_index, pages)
    start = index * size
    end = min(start + size, max(0, count))
    return start, end, index, pages


def page_target(action, current_index, total_pages):
    current = clamp_page_index(current_index, total_pages)
    last = max(0, int(total_pages or 1) - 1)
    if action == "first":
        return 0
    if action == "prev":
        return max(0, current - 1)
    if action == "next":
        return min(last, current + 1)
    if action == "last":
        return last
    return None


def parse_search_tokens(raw_text):
    """Parse dialogue search text into token dictionaries plus a speaker filter."""

    if not raw_text:
        return [], ""

    tokens = []
    speaker_filter = ""
    pos = 0
    text = str(raw_text or "").strip()
    n = len(text)

    while pos < n:
        while pos < n and text[pos].isspace():
            pos += 1
        if pos >= n:
            break

        ch = text[pos]
        if ch == '"':
            end = text.find('"', pos + 1)
            if end == -1:
                phrase = text[pos + 1:].strip().lower()
                pos = n
            else:
                phrase = text[pos + 1:end].strip().lower()
                pos = end + 1
            if phrase:
                tokens.append({"type": "phrase", "terms": [phrase]})
            continue

        end = pos
        while end < n and not text[end].isspace():
            end += 1
        raw = text[pos:end]
        pos = end
        lower = raw.lower()

        if lower.startswith("speaker:"):
            val = raw[8:].strip(' [](){}"').upper()
            if val:
                speaker_filter = val
            continue
        if raw.startswith("@"):
            val = raw[1:].strip(' [](){}"').upper()
            if val:
                speaker_filter = val
            continue
        if raw.startswith("[") and raw.endswith("]") and len(raw) > 2:
            val = raw[1:-1].strip().upper()
            if val and not val.isdigit():
                speaker_filter = val
                continue

        if lower.startswith("id:"):
            val = raw[3:].strip().lower()
            if val:
                tokens.append({"type": "id", "terms": [val]})
            continue

        if raw.startswith("-") and len(raw) > 1:
            val = lower[1:]
            if val:
                tokens.append({"type": "not", "terms": [val]})
            continue

        if "|" in raw:
            parts = [p.strip().lower() for p in raw.split("|") if p.strip()]
            if parts:
                tokens.append({"type": "or", "terms": parts})
            continue

        if lower:
            tokens.append({"type": "and", "terms": [lower]})

    return tokens, speaker_filter


def search_text_from_tokens(tokens):
    clean_parts = []
    for token in tokens or []:
        token_type = token.get("type")
        terms = token.get("terms") or []
        if not terms:
            continue
        if token_type in {"and", "phrase", "id"}:
            clean_parts.append(str(terms[0]))
        elif token_type == "or":
            clean_parts.append("|".join(str(term) for term in terms))
    return " ".join(clean_parts)


def parse_search_text(raw_text):
    tokens, speaker = parse_search_tokens(raw_text)
    return search_text_from_tokens(tokens), speaker


def matches_search_tokens(blob, speaker, search_tokens, speaker_filter):
    if speaker_filter and str(speaker or "") != str(speaker_filter or ""):
        return False

    blob = str(blob or "")
    for token in search_tokens or []:
        token_type = token.get("type")
        terms = token.get("terms") or []
        if not terms:
            continue
        if token_type in {"and", "phrase"}:
            if terms[0] not in blob:
                return False
        elif token_type == "not":
            if terms[0] in blob:
                return False
        elif token_type == "id":
            if not blob.startswith(terms[0]):
                return False
        elif token_type == "or":
            if not any(term in blob for term in terms):
                return False
    return True


def simple_search_terms(search_text):
    return [token for token in str(search_text or "").strip().lower().split() if token]


def matches_simple_search(blob, search_terms):
    blob = str(blob or "")
    return all(term in blob for term in search_terms or [])


def filter_items_simple(items, *, search_text="", speaker_filter="", blob_key="search_blob", speaker_key="speaker"):
    search_terms = simple_search_terms(search_text)
    speaker_filter = str(speaker_filter or "").strip().upper()
    if not search_terms and not speaker_filter:
        return list(items)

    out = []
    for item in items:
        if speaker_filter and get_item_value(item, speaker_key, "") != speaker_filter:
            continue
        if search_terms and not matches_simple_search(get_item_value(item, blob_key, ""), search_terms):
            continue
        out.append(item)
    return out


def normalize_repo_path(value):
    return str(value or "").strip().replace("/", "\\").lstrip("\\").lower()


def normalize_resource_filter_value(value):
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.search(r'"([^"]+)"', text)
    if match:
        text = match.group(1).strip()
    text = text.replace("/", "\\").lstrip("\\").lower()
    return re.sub(r"\s+", " ", text)


def item_scene_paths(item, *, scene_key="scene_path", source_scenes_key="source_scenes"):
    paths = []
    raw_paths = get_item_value(item, source_scenes_key, []) or []
    if isinstance(raw_paths, str):
        raw_paths = [raw_paths]
    for path in raw_paths:
        norm = normalize_repo_path(path)
        if norm and norm not in paths:
            paths.append(norm)
    primary = normalize_repo_path(get_item_value(item, scene_key, ""))
    if primary and primary not in paths:
        paths.insert(0, primary)
    return paths


def item_matches_scene_filter(item, scene_filter, *, scene_key="scene_path", source_scenes_key="source_scenes"):
    scene_filter = normalize_repo_path(scene_filter)
    if not scene_filter:
        return True
    return scene_filter in item_scene_paths(
        item,
        scene_key=scene_key,
        source_scenes_key=source_scenes_key,
    )


def item_matches_resource_filter(
    item,
    resource_filter,
    *,
    resource_key="resource",
    scene_key="scene_path",
    source_scenes_key="source_scenes",
):
    resource_filter = normalize_resource_filter_value(resource_filter)
    if not resource_filter:
        return True

    candidates = [
        get_item_value(item, resource_key, ""),
        get_item_value(item, scene_key, ""),
    ]
    raw_paths = get_item_value(item, source_scenes_key, []) or []
    if isinstance(raw_paths, str):
        raw_paths = [raw_paths]
    candidates.extend(raw_paths)

    for value in candidates:
        norm = normalize_resource_filter_value(value)
        if norm and (norm == resource_filter or resource_filter in norm):
            return True
    return False
