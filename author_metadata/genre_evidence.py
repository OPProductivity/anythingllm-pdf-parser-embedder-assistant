"""Small evidence operations shared by independent publication strategies."""

import re
import unicodedata

from rag_pdf_tools import normalize_text
from .names import split_author_line_candidates, normalize_author_candidate, looks_like_person_name


def opening_lines(context):
    for sample in context.opening_pages:
        yield int(sample['page']), [normalize_text(line) for line in
                                  str(sample.get('text') or '').splitlines()
                                  if normalize_text(line)]


def empty_result():
    return {'author': '', 'source': 'not_found', 'page': 0, 'evidence': ''}


def credited_names(value):
    names = split_author_line_candidates(value, title_hint='', allow_all_caps=True,
                                        require_complete=True)
    if names:
        return names
    name = normalize_author_candidate(value)
    if looks_like_person_name(name, title_hint='', allow_all_caps=True):
        return [name]
    return []


def result(context, names, source, page, evidence):
    """Apply sampled translator-role evidence even to an early strong credit."""
    excluded = set()
    for _, lines in opening_lines(context):
        for line in lines:
            match = re.match(r'^translated\s+by\s+(.+)$', line, flags=re.I)
            if match:
                excluded.update(unicodedata.normalize('NFC', n).casefold()
                                for n in credited_names(match.group(1)))
    retained = [n for n in names if unicodedata.normalize('NFC', n).casefold() not in excluded]
    if not retained:
        return empty_result()
    return {'author': ', '.join(retained), 'source': source,
            'page': page, 'evidence': evidence}
