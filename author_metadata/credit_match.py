"""Corroborate a filename clue with an explicit credit on an opening PDF page."""

import re
import unicodedata

from rag_pdf_tools import normalize_text

from .names import looks_like_person_name, normalize_author_candidate


def _fold(value):
    decomposed = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(char for char in decomposed if not unicodedata.combining(char)).casefold()


def _filename_contains_surname(path, surname):
    stem_words = re.findall(r"[^\W\d_]+", _fold(path.stem), flags=re.UNICODE)
    surname_words = re.findall(r"[^\W\d_]+", _fold(surname), flags=re.UNICODE)
    return bool(surname_words and all(word in stem_words for word in surname_words))


def match_filename_to_pdf_author_credit(context):
    """Accept only a printed Author field corroborated by the filename.

    A matching body mention, citation, affiliation, or editor field is never a
    credit. Limit this recovery to opening-page work records; whole issues and
    chapter/decision roles need their specialized attribution rules.
    """
    if context.path is None or context.profile.kind in {
        "periodical_issue", "book_chapter", "legal_opinion", "review",
    } or context.profile.work_scope == "whole_issue":
        return None
    matches = []
    for sample in context.opening_pages:
        try:
            page = int(sample.get("page") or 0)
        except (ValueError, TypeError):
            continue
        lines = [normalize_text(line) for line in str(sample.get("text") or "").splitlines()]
        nonempty = [line for line in lines if line]
        for index, line in enumerate(nonempty[:45]):
            labelled = re.fullmatch(r"authors?(?:\(s\))?\s*[:\-]?\s*(.*)", line, re.I)
            if not labelled:
                continue
            value = labelled.group(1).strip()
            if not value and index + 1 < len(nonempty):
                value = nonempty[index + 1]
            if not value or len(value) > 90:
                continue
            author = normalize_author_candidate(value)
            if not looks_like_person_name(author, allow_all_caps=True):
                continue
            surname = author.split()[-1].strip(".,")
            if _filename_contains_surname(context.path, surname):
                matches.append((author, page, line, value))
    distinct = {(_fold(author), page) for author, page, _, _ in matches}
    if len({_fold(author) for author, _, _, _ in matches}) != 1 or not distinct:
        return None
    author, page, label, value = matches[0]
    return {"author": author, "source": "filename_pdf_credit_match", "page": page,
            "evidence": f"{label} / {value}" if not label.endswith(value) else label}


def match_filename_to_scholarly_byline(context):
    """Match a first-page standalone scholarly byline to a filename surname."""
    if (context.path is None or context.profile.kind != "scholarly_article"
            or context.profile.work_scope != "single_work"):
        return None
    matches = []
    for sample in context.opening_pages:
        if int(sample.get("page") or 0) != 1:
            continue
        lines = [normalize_text(line) for line in str(sample.get("text") or "").splitlines()]
        lines = [line for line in lines if line]
        for index, raw in enumerate(lines[:18]):
            value = re.sub(r"[\d\ue000-\uf8ff*†‡§¶]+$", "", raw).strip()
            if not re.fullmatch(r"[^\W\d_][\w .’'\-]{5,78}", value, re.UNICODE):
                continue
            name = normalize_author_candidate(value)
            if name.islower():
                name = name.title()
            if not looks_like_person_name(name, allow_all_caps=False):
                continue
            if not _filename_contains_surname(context.path, name.split()[-1]):
                continue
            following = " ".join(lines[index + 1:index + 4]).casefold()
            nearby_heading = any(
                len(re.findall(r"[^\W\d_]+", line, re.UNICODE)) >= 2
                and line.isupper()
                and not re.search(r"\b(?:ISSN|DOI|VOL|VOLUME|NO)\b", line, re.I)
                for line in lines[index + 1:index + 3]
            )
            if not (re.search(r"\b(?:abstract|university|college|institute|department)\b", following)
                    or nearby_heading):
                continue
            matches.append(name)
    if len({_fold(name) for name in matches}) != 1:
        return None
    return {"author": matches[0], "source": "filename_pdf_standalone_match",
            "page": 1, "evidence": "opening-page standalone byline matched filename surname"}
