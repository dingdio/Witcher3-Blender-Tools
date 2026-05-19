import re
from dataclasses import dataclass
from pathlib import Path


SILENCE_PHONEMES = {"SIL", "SP", "SPN"}


@dataclass(frozen=True)
class PhonemeSegment:
    phoneme: str
    start_ms: int
    end_ms: int
    weight: float = 1.0
    status: str = ""
    raw_phone: str = ""

    @property
    def duration_ms(self):
        return max(0, self.end_ms - self.start_ms)


def _parse_int(value):
    return int(str(value).strip())


def _parse_float(value, default=1.0):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def read_ipa_similarity_map(path, allowed_phonemes=None):
    """Read Radish IPA/eSpeak to ARPAbet similarity data and choose best ARPAbet."""
    path = Path(path)
    if not path.is_file():
        return {}

    allowed = {str(phone).upper() for phone in (allowed_phonemes or [])}
    headers = []
    result = {}
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith(";"):
                continue

            parts = [part.strip() for part in line.split("|")]
            if not parts:
                continue

            if not headers:
                headers = [part.upper() for part in parts[1:]]
                continue

            ipa = parts[0]
            best_phone = ""
            best_score = 0.0
            for phone, score_text in zip(headers, parts[1:]):
                if allowed and phone not in allowed:
                    continue
                score = _parse_float(score_text, default=0.0)
                if score > best_score:
                    best_phone = phone
                    best_score = score
            if ipa and best_phone and best_score > 0.0:
                result[ipa] = best_phone
                result[ipa.upper()] = best_phone
    return result


def _extract_arpabet(parts, allowed, ipa_map=None):
    raw_phone = (parts[0] if parts else "").strip()
    timing_text = parts[6] if len(parts) > 6 else ""
    ipa_map = ipa_map or {}

    match = re.search(r"~\s*([A-Z0-9]+)\b", timing_text)
    if match:
        phone = match.group(1).upper()
        if phone not in SILENCE_PHONEMES and (not allowed or phone in allowed):
            return phone, raw_phone

    mapped_phone = ipa_map.get(raw_phone) or ipa_map.get(raw_phone.upper())
    if mapped_phone:
        mapped_phone = mapped_phone.upper()
        if mapped_phone not in SILENCE_PHONEMES and (not allowed or mapped_phone in allowed):
            return mapped_phone, raw_phone

    raw_upper = raw_phone.upper()
    if raw_upper not in SILENCE_PHONEMES and (not allowed or raw_upper in allowed):
        return raw_upper, raw_phone

    for token in re.findall(r"\b[A-Z0-9]{1,4}\b", timing_text):
        token = token.upper()
        if token not in SILENCE_PHONEMES and (not allowed or token in allowed):
            return token, raw_phone

    return "", raw_phone


def parse_phoneme_file(path, allowed_phonemes=None, ipa_map=None):
    """Parse a Radish/w3.phoneme-extractor .phonemes file into ARPAbet segments."""
    path = Path(path)
    allowed = {str(phone).upper() for phone in (allowed_phonemes or [])}
    segments = []

    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith(";") or "|" not in line:
                continue

            parts = [part.strip() for part in line.split("|")]
            if len(parts) < 4:
                continue

            phoneme, raw_phone = _extract_arpabet(parts, allowed, ipa_map=ipa_map)
            if not phoneme:
                continue

            try:
                start_ms = _parse_int(parts[1])
                end_ms = _parse_int(parts[2])
            except ValueError:
                continue
            if end_ms <= start_ms:
                continue

            weight = max(0.0, min(1.0, _parse_float(parts[3], default=1.0)))
            status = parts[5] if len(parts) > 5 else ""
            segments.append(
                PhonemeSegment(
                    phoneme=phoneme,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    weight=weight,
                    status=status,
                    raw_phone=raw_phone,
                )
            )

    return segments
