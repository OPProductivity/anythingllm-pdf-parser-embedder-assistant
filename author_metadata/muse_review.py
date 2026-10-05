"""Project MUSE review-cover credits, kept separate from reviewed-work names."""

import re
import unicodedata

from .names import has_author_affiliation_hint, looks_like_person_name, normalize_author_candidate
from .title_evidence import words


def covers(samples):
    for sample in samples:
        if int(sample.get("page") or 0) != 1:
            continue
        lines = [" ".join(line.split()) for line in sample["text"].splitlines() if line.strip()]
        if not any(re.fullmatch(r"https?://muse\.jhu\.edu/article/\d+", line) for line in lines[:24]):
            continue
        for index, line in enumerate(lines[:12]):
            if not line.startswith("Published by ") or index < 3:
                continue
            for width in (1, 2, 3):
                start = index - width
                if start < 2:
                    continue
                publication = " ".join(lines[start:index])
                if not re.fullmatch(
                    r".+, Volume \d+, Number \d+,.+\bpp\. \d+[-\u2013]\d+ \(Review\)",
                    publication,
                ):
                    continue
                yield " ".join(lines[:start - 1]), lines[start - 1], lines, start - 1


def _repaired_name(raw):
    if not re.search(r"[\u00c3\u00c2][\u0080-\u00bf]|\u00e2[\u0080-\u00bf\u20ac]", raw):
        if re.search(r"\w[\u00c3\u00c2]", raw):
            return "", False
        return unicodedata.normalize("NFC", raw), False
    for codec in ("latin1", "cp1252"):
        try:
            repaired = raw.encode(codec).decode("utf8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if repaired != raw and not any(marker in repaired for marker in ("\u00c3", "\u00c2", "\u00e2")):
            return unicodedata.normalize("NFC", repaired), True
    return "", False


def _corroborated_signature(context, name, title):
    # A new reviewed-work citation ends the evidence scope of this review.
    original = words(re.split(r"\s+by\s+", title, flags=re.I)[0])
    lines = []
    for sample in context.opening_pages:
        if int(sample["page"]) > 1:
            lines.extend(" ".join(line.split()) for line in sample["text"].splitlines() if line.strip())
    for index, line in enumerate(lines):
        if (has_author_affiliation_hint(line) and index + 1 < len(lines)
                and unicodedata.normalize("NFC", lines[index + 1]) == name):
            return True
        citation = re.search(r"^(.{12,400}?)\.\s+By\s+[^.]{4,100}\.\s+\(", " ".join(lines[index:index + 2]))
        if citation:
            cited = words(citation.group(1))
            if not (cited == original or cited[-len(original):] == original
                    or (len(cited) >= 3 and original[-len(cited):] == cited)):
                return False
        if unicodedata.normalize("NFC", line) == name and index and has_author_affiliation_hint(lines[index - 1]):
            return True
    return False


def infer_cover(context):
    for title, raw, lines, index in covers(context.opening_pages):
        if words(title) != words(context.title_hint):
            continue
        name, repaired = _repaired_name(raw)
        if not name or not looks_like_person_name(name, title_hint=title, allow_all_caps=False):
            continue
        if normalize_author_candidate(name) != name:
            continue
        if not all(char.isalpha() or char in " .'-\u2019" for char in name):
            continue
        if repaired and not _corroborated_signature(context, name, title):
            continue
        return name, " / ".join(lines[:index + 4])
    return "", ""
