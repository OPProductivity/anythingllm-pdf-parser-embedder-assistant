"""Review-specific author-role and reviewed-work distinctions."""

import re

from rag_pdf_tools import normalize_text

from .genre_evidence import credited_names, empty_result, opening_lines, result
from .names import has_author_affiliation_hint, looks_like_person_name, normalize_author_candidate, looks_like_review_heading


def infer_review_author(context):
    """Review creators precede any people named in reviewed-work citations."""
    for page, lines in opening_lines(context):
        names, evidence = extract_affiliated_review_credit(lines)
        if names:
            return result(context, names, 'text_review_byline', page, evidence)
        for index, line in enumerate(lines[:28]):
            match = re.fullmatch(r'^(?:review(?:ed)?\s+by|reviewers?)\s*[:\-]?\s*(.+)', line, flags=re.I)
            if match:
                names = credited_names(match.group(1))
                if names:
                    return result(context, names, 'text_review_byline', page, line)
    for page, lines in opening_lines(context):
        for index, line in enumerate(lines[:32]):
            match = re.match(r'^To cite this article:\s*(.+?)\s*\(\d{4}\)(?:\s*:|\s+\S)', line, flags=re.I)
            if match and index:
                names = credited_names(match.group(1))
                if names and normalize_text(lines[index - 1]) == normalize_text(match.group(1)):
                    return result(context, names, 'text_bibliographic_byline', page, lines[index - 1] + ' / ' + line)
    for page, lines in opening_lines(context):
        names, evidence = extract_heading_review_credit(lines)
        if names:
            return result(context, names, 'text_stacked_affiliated_byline', page, evidence)
    # A journal review's title/byline/citation block supplies role evidence.
    # Every intervening line must belong to the visible title or affiliation
    # block; an inserted participant heading therefore invalidates the layout.
    title_key = ' '.join(re.findall(r'\w+', context.title_hint.casefold()))
    for page, lines in opening_lines(context):
        for start, heading in enumerate(lines[:24]):
            if not looks_like_review_heading(heading):
                continue
            for citation in range(start + 3, min(start + 12, len(lines))):
                if not is_reviewed_work_citation(lines[citation]):
                    continue
                names = []
                first_credit = citation
                for index in range(citation - 1, start, -1):
                    name, sep, affiliation = lines[index].partition(',')
                    candidate = normalize_author_candidate(name)
                    if not (sep and has_author_affiliation_hint(affiliation)
                            and looks_like_person_name(candidate, title_hint='', allow_all_caps=False)):
                        break
                    names.insert(0, candidate)
                    first_credit = index
                visible_title = ' '.join(re.findall(r'\w+', ' '.join(lines[start + 1:first_credit]).casefold()))
                if (names and len(visible_title.split()) >= 5 and title_key
                        and visible_title in title_key):
                    return result(context, names, 'text_review_affiliated_byline', page,
                                  ' / '.join(lines[start:citation + 1]))
    return empty_result()


def extract_heading_review_credit(lines):
    """A reviewer directly follows the genre heading and precedes affiliation."""
    values = [normalize_text(line) for line in (lines or []) if normalize_text(line)]
    for index, line in enumerate(values[:24]):
        if not looks_like_review_heading(line) or index + 2 >= len(values):
            continue
        raw_name = values[index + 1]
        if re.match(r'^(?:by|edited|curated|directed|translated|produced)\b', raw_name, re.I):
            continue
        candidate = normalize_author_candidate(raw_name)
        affiliation = values[index + 2]
        independent_contact = bool(
            re.fullmatch(r'independent\s+(?:scholar|researcher|writer)(?:\s+and\s+(?:scholar|researcher|writer))?\s*,?',
                         affiliation, flags=re.I)
            and any(re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', value)
                    for value in values[index + 3:index + 5])
        )
        if (looks_like_person_name(candidate, title_hint='', allow_all_caps=False)
                and (has_author_affiliation_hint(affiliation, max_chars=180) or independent_contact)):
            return [candidate], ' / '.join(values[index:index + 3])
    return [], ''


def is_reviewed_work_citation(value):
    return bool(re.match(
        r"^(?:books?|films?|documentaries?|exhibitions?|performances?|works?|articles?)\s+reviewed\s*:",
        normalize_text(value or ""), flags=re.I,
    ))


def extract_affiliated_review_credit(lines):
    """Read a wrapped, explicitly labelled review byline with affiliations."""
    values = [normalize_text(line) for line in (lines or []) if normalize_text(line)]
    name_token = r"[A-ZÀ-ÖØ-Þ][^\s,]+"
    next_name = rf"{name_token}(?:\s+{name_token}){{1,4}},"
    for index, line in enumerate(values[:24]):
        match = re.match(r"^reviewed\s+by\s*:?\s*(.+)$", line, flags=re.I)
        if not match or not any(
            looks_like_review_heading(heading)
            for heading in values[max(0, index - 5):index]
        ):
            continue
        pieces = re.split(rf"\s+and\s+(?={next_name})", match.group(1))
        if not 1 <= len(pieces) <= 8:
            continue
        names = []
        used_following_affiliation = False
        for position, piece in enumerate(pieces):
            name, separator, affiliation = piece.partition(",")
            candidate = normalize_author_candidate(name)
            if not separator or not looks_like_person_name(candidate, title_hint="", allow_all_caps=False):
                break
            if not affiliation.strip() and position == len(pieces) - 1:
                affiliation = values[index + 1] if index + 1 < len(values) else ""
                used_following_affiliation = True
            if not has_author_affiliation_hint(affiliation, max_chars=180):
                break
            names.append(candidate)
        if len(names) == len(pieces):
            evidence_lines = values[index:index + 2] if used_following_affiliation else [line]
            return names, " / ".join(evidence_lines)
    return [], ""
