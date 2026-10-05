"""Report-specific author credits, distinct from recipient and sponsor roles."""

import re

from rag_pdf_tools import normalize_text

from .genre_evidence import credited_names, empty_result, opening_lines, result
from .names import normalize_author_candidate, looks_like_person_name


def infer_report_author(context):
    """Report preparation and author labels precede title-adjacent recipients."""
    for pattern, source in (
        (r'^prepared\s+by\s*:?\s*(.*)$', 'text_report_prepared_by'),
        (r'^(?:report\s+)?authors?\s*:\s*(.*)$', 'text_author_label'),
        (r'^written\s+by\s*:?\s*(.*)$', 'text_written_by'),
        (r'^by\s+(.+)$', 'text_byline'),
    ):
        for page, lines in opening_lines(context):
            for index, line in enumerate(lines[:28]):
                match = re.fullmatch(pattern, line, flags=re.I)
                if not match:
                    continue
                credit = match.group(1) or (lines[index + 1] if index + 1 < len(lines) else '')
                names = credited_names(credit)
                if names:
                    return result(context, names, source, page, line + ' / ' + credit)
    return empty_result()


def extract_prepared_by_credit(lines):
    values = [normalize_text(line) for line in (lines or []) if normalize_text(line)]
    for index, line in enumerate(values[:28]):
        match = re.fullmatch(r"prepared\s+by\s*:?(?:\s+(.+))?", line, flags=re.I)
        if not match:
            continue
        credit = normalize_text(match.group(1) or (values[index + 1] if index + 1 < len(values) else ""))
        name = normalize_author_candidate(credit)
        if looks_like_person_name(name, title_hint="", allow_all_caps=True):
            return name, f"{line} / {credit}" if not match.group(1) else line
    return "", ""
