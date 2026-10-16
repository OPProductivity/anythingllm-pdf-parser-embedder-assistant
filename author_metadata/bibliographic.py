"""Repeated ebook-platform work credits on downloaded book excerpts."""

import re
import unicodedata

from .names import looks_like_person_name, normalize_author_candidate
from .title_evidence import not_found, words


def _fold(value):
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(character)
    )


def _title_agrees(printed, hint):
    visible = tuple(_fold(token) for token in words(printed))
    expected = tuple(_fold(token) for token in words(re.split(r"\s+--\s+", hint)[0]))
    if len(expected) < 3:
        return False
    # Truncated export names still preserve the opening title phrase.
    prefix = min(len(expected), 5)
    return any(
        visible[:prefix] == expected[start : start + prefix]
        for start in range(len(expected) - prefix + 1)
    )


def _title_key(value):
    return " ".join(_fold(token) for token in words(value))


def infer_repeated_platform_credit(context):
    if context.profile.kind not in {"book_chapter", "generic", "scholarly_article"}:
        return not_found()
    hits = {}
    for sample in context.opening_pages:
        text = str(sample.get("text") or "")
        page = int(sample.get("page") or 0)
        for line in text.splitlines():
            if "AN:" in line and "EBSCO" in text:
                match = re.search(
                    r"\bAN:\s*\d+\s*;\s*([^;\n]{5,80})\s*;\s*([^;\n]{8,150})", line
                )
                if match:
                    author = normalize_author_candidate(match.group(1).rstrip(" ."))
                    title = match.group(2)
                    # A short chapter export name such as `Veblen 4` lacks the
                    # parent-book title; its surname must match the catalog.
                    short_hint = re.fullmatch(
                        r"\s*([^\s]+)\s+\d+\s*", context.title_hint
                    )
                    surname_match = bool(
                        short_hint
                        and _fold(short_hint.group(1)) == _fold(author.split()[-1])
                    )
                    if not (surname_match or _title_agrees(title, context.title_hint)):
                        continue
                else:
                    continue
            else:
                continue
            if not looks_like_person_name(author):
                continue
            hits.setdefault((_fold(author), _title_key(title)), []).append(
                (page, author, line.strip())
            )
        compact = " ".join(text.replace("\u00ad", "").split())
        if "ProQuest Ebook Central" in compact:
            match = re.search(
                r"\b([A-ZÀ-ÖØ-Þ][\w\u00c0-\u024f\u2019-]{2,40}),\s*"
                r"([A-ZÀ-ÖØ-Þ][\w\u00c0-\u024f.\s-]{2,55}?)\.\s+"
                r"(.{8,180}?),\s*[^,]{3,80},\s*(?:18|19|20)\d{2}\.\s*ProQuest Ebook Central",
                compact,
                re.I,
            )
            if match:
                author = normalize_author_candidate(
                    match.group(1) + ", " + match.group(2)
                )
                title = match.group(3)
                if looks_like_person_name(author) and _title_agrees(
                    title, context.title_hint
                ):
                    hits.setdefault((_fold(author), _title_key(title)), []).append(
                        (page, author, " ".join(match.group(0).split())[:250])
                    )
    repeated = [
        entries for entries in hits.values() if len({item[0] for item in entries}) >= 2
    ]
    if len(repeated) != 1:
        return not_found()
    page, author, line = repeated[0][0]
    return {
        "author": author,
        "source": "text_bibliographic_author_label",
        "page": page,
        "evidence": line[:250],
    }
