"""Scholarly title/byline/affiliation/abstract grammar.

Names in subject, interview, contributor, or body positions are not credits.
This strategy does not infer journal authors from PDF properties or filenames.
"""

import re
import unicodedata

from .names import (normalize_author_candidate, looks_like_person_name,
                    split_author_line_candidates, has_author_affiliation_hint)
from .title_evidence import matches, not_found, words

_ROLE = re.compile(r"\b(?:interviewees?|interview\s+with|participants?|contributors?|"
                   r"speakers?|series\s+editors?|volume\s+editors?|editorial\s+board|"
                   r"translators?|translated\s+by|acknowledgments?|acknowledgements?|references|bibliography)\b", re.I)
_BOUNDARY = re.compile(r"^(?:abstract|keywords?|introduction)\b", re.I)
_LOCATION = re.compile(r"^[^,\d]{2,40},\s*[^,\d]{2,40}$")
_DOI = re.compile(r"^(?:doi\s*:\s*|https?://(?:dx\.)?doi\.org/)10\.\S+$", re.I)
_PUBLICATION = re.compile(r"^(?:received:|revised:|accepted:|published online:|\u00a9\s|copyright\s)", re.I)
_DATE = re.compile(r"^\d{1,2}\s+[A-Za-z]+\s+\d{4}$")


def _names(line, title=""):
    if _ROLE.search(line) or has_author_affiliation_hint(line):
        return []
    # A comma is an author-list delimiter only with a visible conjunction;
    # this keeps `Aalborg, Denmark` out of inverted-name normalization.
    if "," in line and not re.search(r"\band\b|&", line, re.I):
        return []
    line = re.sub(r"(?<=[^\W\d_])[\d*\u2020\u2021]+(?=\s*(?:,|&|\band\b|$))", "", line)
    names = split_author_line_candidates(line, title_hint=title, require_complete=True)
    if names:
        return names
    candidate = normalize_author_candidate(line)
    return [candidate] if looks_like_person_name(candidate, title_hint=title) else []


def _matches_title(visible, hint, names):
    if matches(visible, hint, names, allow_long_prefix=True):
        return True
    parts = re.split(r"\s+(?:--|[-\u2013\u2014])\s+", hint)
    if len(parts) > 1 and sorted(words(parts[0])) == sorted(words(" ".join(names))):
        return matches(visible, " ".join(parts[1:]), names, allow_long_prefix=True)
    return False


def _result(names, sample, lines, source):
    return {"author": ", ".join(names), "source": source,
            "page": int(sample["page"]), "evidence": " / ".join(lines)}


def _cover_credit(lines, context, sample):
    for index, line in enumerate(lines[:24]):
        labelled = re.fullmatch(r"Author\(s\):\s*(.+)", line, re.I)
        if labelled and index and any(value.startswith("Source:") for value in lines[index + 1:index + 4]):
            names = _names(labelled.group(1))
            archive = any(re.match(r"Stable URL:\s*https?://(?:www\.)?jstor\.org/", value, re.I)
                          for value in lines[index + 1:index + 6])
            title_ok = any(
                (len(words(visible)) >= 2 and words(visible) == words(context.title_hint))
                or _matches_title(visible, context.title_hint, names)
                for visible in (" ".join(lines[index - width:index])
                                for width in range(1, min(index, 5) + 1)))
            if names and archive and title_ok and not any(_ROLE.search(value) for value in lines[:index]):
                return _result(names, sample, lines[index:index + 3], "text_author_label")
        names = _names(line)
        if not names or any(_ROLE.search(value) for value in lines[:index]):
            continue
        title_ok = any(_matches_title(" ".join(lines[index - width:index]), context.title_hint, names)
                       for width in range(1, min(index, 5) + 1))
        if not title_ok:
            continue
        following = lines[index + 1:index + 5]
        if following and re.match(r"^To cite this article:\s*", following[0], re.I):
            citation_names = re.split(r"\s*\(\d{4}\)", re.sub(r"^To cite this article:\s*", "", following[0], flags=re.I), maxsplit=1)[0]
            if _names(citation_names) == names:
                return _result(names, sample, [line, following[0]], "text_bibliographic_byline")
        if (any(re.search(r"\(Article\)\s*$", value) for value in following)
                and any(re.match(r"^Published by\s+", value) for value in following)
                and any(re.match(r"https?://muse\.jhu\.edu/article/", value) for value in lines[index + 1:index + 8])):
            return _result(names, sample, [line] + following, "text_strict_credit_block")
    return not_found()


def _translated_credit(lines, context, sample):
    if not any(re.search(r"\bVol\.?\s*\d+.*\bIssue\b", value, re.I) for value in lines[:6]):
        return not_found()
    for index, line in enumerate(lines[:20]):
        if not re.fullmatch(r"Translated by\s*:?", line, re.I) or index < 2:
            continue
        if any(_ROLE.search(value) for value in lines[:index]):
            continue
        names = _names(lines[index - 1])
        if not names:
            continue
        for width in range(1, min(index - 1, 5) + 1):
            visible = " ".join(lines[index - 1 - width:index - 1])
            visible = re.sub(r"(?<=[^\W\d_])\d{1,2}$", "", visible)
            if _matches_title(visible, context.title_hint, names):
                return _result(names, sample, [lines[index - 1], line], "text_title_byline_translation")
    return not_found()


def infer(context):
    if context.profile.kind != "scholarly_article":
        return not_found()
    for sample in context.opening_pages:
        if int(sample["page"]) > 3:
            continue
        lines = [" ".join(unicodedata.normalize("NFKC", line).split()) for line in sample["text"].splitlines() if line.strip()][:100]
        cover = _cover_credit(lines, context, sample)
        if cover["author"]:
            return cover
        translated = _translated_credit(lines, context, sample)
        if translated["author"]:
            return translated
        if any(_BOUNDARY.match(line) for line in lines[:48]):
            for line in lines[:16]:
                explicit = re.fullmatch(r"(?:by|written\s+by|authors?\s*:)\s*(.+)", line, re.I)
                if explicit:
                    names = _names(explicit.group(1))
                    if names:
                        return _result(names, sample, [line], "text_byline")
        for start in range(min(80, len(lines))):
            if any(_ROLE.search(line) for line in lines[:start + 1]):
                continue
            names, credit_lines = [], []
            cursor = start
            affiliation = False
            while cursor < min(len(lines), start + 12):
                line = lines[cursor]
                if _BOUNDARY.match(line) or _ROLE.search(line):
                    break
                inline = re.fullmatch(r"([^,]{3,80}),\s*(.+)", line)
                parenthetical = re.fullmatch(r"(.{3,80}?)\s*\(([^)]{3,100})\)", line)
                if parenthetical and has_author_affiliation_hint(parenthetical.group(2)):
                    found = _names(parenthetical.group(1))
                    affiliation = True
                elif (inline and has_author_affiliation_hint(inline.group(2))
                      and _names(inline.group(1))):
                    found = _names(inline.group(1))
                    affiliation = True
                elif has_author_affiliation_hint(line) or re.search(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", line):
                    if not names:
                        break
                    affiliation = True
                    credit_lines.append(line)
                    cursor += 1
                    continue
                elif names and (_PUBLICATION.match(line) or _DATE.fullmatch(line)):
                    credit_lines.append(line)
                    cursor += 1
                    continue
                elif _LOCATION.fullmatch(line):
                    # One location after an established name may precede the
                    # abstract. It is never itself parsed as a person.
                    if not names or cursor + 1 >= len(lines) or not _BOUNDARY.match(lines[cursor + 1]):
                        break
                    credit_lines.append(line)
                    cursor += 1
                    continue
                else:
                    found = _names(line)
                if not found:
                    break
                names.extend(name for name in found if name not in names)
                credit_lines.append(line)
                cursor += 1
            if not names or cursor >= len(lines):
                continue
            boundary = bool(_BOUNDARY.match(lines[cursor]))
            unlabelled = affiliation and len(" ".join(lines[cursor:cursor + 2]).split()) >= 18
            if not (boundary or unlabelled):
                continue
            title_end = start
            if title_end and _DOI.fullmatch(lines[title_end - 1]):
                title_end -= 1
            for width in range(1, min(5, title_end) + 1):
                visible_title = " ".join(lines[title_end - width:title_end])
                if not _matches_title(visible_title, context.title_hint, names):
                    continue
                if not all(looks_like_person_name(name, title_hint=visible_title) for name in names):
                    continue
                return {"author": ", ".join(names), "source": "text_affiliated_byline" if affiliation else "text_strict_credit_block",
                        "page": int(sample["page"]), "evidence": " / ".join(credit_lines + [lines[cursor]])}
    return not_found()
