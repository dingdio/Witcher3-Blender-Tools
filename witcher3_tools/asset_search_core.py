def normalize(text):
    return str(text).lower().replace("/", "\\").replace("_", " ")


def query_tokens(query):
    # Split on whitespace before normalizing so "c_01_wa__body" stays one contiguous phrase.
    return [token for token in (normalize(part).lstrip("\\") for part in str(query or "").split()) if token]


def match_rank(path, tokens, phrase):
    """Rank a normalized path that contains every token; lower is better."""
    if path == phrase or path.endswith("\\" + phrase):
        return 0
    base = path.rpartition("\\")[2]
    dot = base.rfind(".")
    stem = path[:len(path) - len(base) + dot] if dot > 0 else path
    if stem == phrase or stem.endswith("\\" + phrase):
        return 1
    if base.startswith(phrase):
        return 2
    if all(token in base for token in tokens):
        return 3
    return 4


def search(entries, query, limit, path_filter=""):
    """Best-first [(rank, payload)] from (normalized_path, payload) entries, plus the total match count."""
    tokens = query_tokens(query)
    if not tokens:
        return [], 0
    phrase = " ".join(tokens)
    path_filter = normalize(path_filter.strip())
    hits = [
        (match_rank(path, tokens, phrase), len(path) - path.rfind("\\"), path, payload)
        for path, payload in entries
        if all(token in path for token in tokens) and path_filter in path
    ]
    hits.sort(key=lambda hit: hit[:3])
    return [(hit[0], hit[3]) for hit in hits[:limit]], len(hits)


def group_by_rank(ranked, ordered):
    """Reorder payloads so better ranks come first, keeping `ordered` within each rank."""
    rank_of = {payload: rank for rank, payload in ranked}
    return sorted(ordered, key=rank_of.__getitem__)
