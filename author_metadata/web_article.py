"""Visible dated bylines on saved or browser-printed articles."""

import re
from datetime import datetime

from rag_pdf_tools import normalize_text

from .genre_evidence import credited_names, empty_result, opening_lines, result
from .names import looks_like_non_person_byline, looks_like_person_name, normalize_author_candidate


def infer_web_article_author(context):
    """A visible editorial byline precedes weaker dated-header interpretation."""
    for page, lines in opening_lines(context):
        if page > 2:
            continue
        for index, line in enumerate(lines[:28]):
            match = re.fullmatch(
                r'^(by|written\s+by|(?:text|article|essay|column|commentary|analysis|opinion)\s+by)\s+(.+)',
                line, flags=re.I,
            )
            if match:
                if index + 1 < len(lines) and re.match(r'^(?:18|19|20)\d{2}\b', lines[index + 1]):
                    continue
                if looks_like_non_person_byline(match.group(2)):
                    return {'author': '', 'source': 'text_non_person_byline',
                            'page': page, 'evidence': line}
                names = credited_names(match.group(2))
                if names:
                    label = match.group(1).casefold()
                    source = ('text_written_by' if label == 'written by' else
                              'text_column_byline' if re.match(r'^(?:column|commentary|analysis|opinion)\b', label)
                              else 'text_byline')
                    return result(context, names, source, page, line)
    for page, lines in opening_lines(context):
        if page != 1:
            continue
        names = extract_dated_opening_byline(lines, title_hint=context.title_hint)
        if names:
            evidence = next((line for line in lines if '|' in line
                             and normalize_author_candidate(line.partition('|')[0]) == names[0]), '')
            if not evidence:
                evidence = next((' / '.join(lines[index:index + 3]) for index, line in enumerate(lines)
                                 if normalize_author_candidate(line) == names[0]), '')
            return result(context, names, 'text_dated_opening_byline', page, evidence)
        names = extract_date_then_byline(lines)
        if names:
            evidence = next((' / '.join(lines[index:index + 4]) for index, line in enumerate(lines)
                             if line == names[0]), names[0])
            return result(context, names, 'text_dated_opening_byline', page, evidence)
    return empty_result()


def extract_date_then_byline(lines):
    """Read a printed news byline after its publication date and UI controls.

    The publication date supplies the boundary: a name in an image caption or
    quoted article body before it cannot become the article author. Accept
    only a compact person line immediately after the date or two known browser
    controls, and require a browser-print footer on the same page.
    """
    values = [normalize_text(line) for line in lines if normalize_text(line)]
    if not any(re.fullmatch(r"https?://\S+\s*\|\s*1/\d+", line) for line in values):
        return []
    month = (r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
             r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)")
    date_patterns = (
        rf"\d{{1,2}}\s+{month}\s+20\d{{2}}",
        rf"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),?\s+\d{{1,2}}\s+{month},?\s+20\d{{2}}(?:\s*[-–]\s*\d{{1,2}}:\d{{2}})?",
    )
    for index, line in enumerate(values[:30]):
        if not any(re.fullmatch(pattern, line, flags=re.I) for pattern in date_patterns):
            continue
        for candidate_index in range(index + 1, min(index + 4, len(values))):
            intervening = values[index + 1:candidate_index]
            if any(value.casefold() not in {"save", "add as preferred on google"}
                   for value in intervening):
                continue
            name = normalize_author_candidate(values[candidate_index])
            if looks_like_person_name(name, allow_all_caps=False):
                return [name]
    return []


def extract_dated_opening_byline(
    lines, title_hint="",
):
    """Recognize a title followed by a complete person/dated web byline."""
    values = [normalize_text(line) for line in (lines or []) if normalize_text(line)]
    hint = normalize_text(title_hint).casefold()
    if not hint:
        return []
    if any(re.search(r"\b(?:series|volume)\s+editors?\b|^editors?$", line, flags=re.I) for line in values[:12]):
        return []
    # Native object order can emit a body drop cap before the title block.
    # Only an isolated letter may precede the independently matched title.
    title_start = 1 if values and len(values[0]) == 1 and values[0].isalpha() else 0
    months = {name: index for index, name in enumerate((
        "january", "february", "march", "april", "may", "june", "july",
        "august", "september", "october", "november", "december",
    ), 1)}
    for index in range(1, min(4, len(values))):
        credit = re.fullmatch(r"([^|]{5,80})\s*\|\s*([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", values[index])
        if not credit or credit.group(2).casefold() not in months:
            continue
        try:
            datetime(int(credit.group(4)), months[credit.group(2).casefold()], int(credit.group(3)))
        except ValueError:
            continue
        visible_title = normalize_text(" ".join(values[title_start:index]))
        title_key = visible_title.casefold()
        if len(re.findall(r"[^\W\d_]+", visible_title)) < 2:
            continue
        if not (title_key == hint or any(hint.startswith(title_key + separator) for separator in (" - ", " | ", " \u2013 ", " \u2014 "))):
            continue
        name = normalize_author_candidate(credit.group(1))
        if (not looks_like_person_name(name, title_hint=title_hint, allow_all_caps=True)
                or "," in credit.group(1)
                or re.search(r"\b(?:and|editors?|publishers?|interviewees?|participants?|speakers?)\b|&", name, flags=re.I)):
            continue
        return [name]
    for date_index in range(1, min(6, len(values) - 1)):
        date = re.fullmatch(
            r"([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,\s*(\d{4})",
            values[date_index], flags=re.I,
        )
        if not date or date.group(1).casefold() not in months:
            continue
        try:
            datetime(int(date.group(3)), months[date.group(1).casefold()], int(date.group(2)))
        except ValueError:
            continue
        name = normalize_author_candidate(values[date_index - 1])
        if not looks_like_person_name(name, title_hint="", allow_all_caps=False):
            continue
        for width in range(1, 4):
            visible_title = normalize_text(" ".join(values[date_index + 1:date_index + 1 + width]))
            title_key = visible_title.casefold()
            if len(re.findall(r"[^\W\d_]+", visible_title)) < 5:
                continue
            if title_key == hint or any(
                hint.startswith(title_key + separator)
                for separator in (" - ", " | ", " \u2013 ", " \u2014 ")
            ):
                return [name]
    return []
