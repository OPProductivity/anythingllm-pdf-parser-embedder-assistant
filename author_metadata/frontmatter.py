"""Compact educational and organized-volume credits on unclassified PDFs."""

import re
import unicodedata

from .names import looks_like_person_name, normalize_author_candidate
from .title_evidence import not_found, words


def _fold(value):
    return ''.join(char for char in unicodedata.normalize('NFKD', value.casefold())
                   if not unicodedata.combining(char))


def _work_title_near(lines, hint):
    expected = tuple(_fold(word) for word in words(re.split(r'\s+--\s+', hint)[0]))
    observed = tuple(_fold(word) for word in words(' '.join(lines)))
    if len(expected) < 3:
        return False
    return any(observed[offset:offset + 3] == expected[:3]
               for offset in range(max(0, len(observed) - 2)))


def infer_generic_frontmatter(context):
    if context.profile.kind != 'generic':
        return not_found()
    for sample in context.samples:
        page = int(sample.get('page') or 0)
        if not 1 <= page <= 8:
            continue
        lines = [' '.join(line.split()) for line in str(sample.get('text') or '').splitlines()
                 if line.strip()]
        if page == 1:
            for index in range(1, min(5, len(lines) - 1)):
                honorary = re.fullmatch(r'(?:Dr|Prof)\.\s+(.{5,75})', lines[index], re.I)
                if not honorary or not re.search(r'\b(?:assignment|syllabus)\b', lines[index + 1], re.I):
                    continue
                name = normalize_author_candidate(honorary.group(1))
                if (_work_title_near(lines[index + 1:index + 3], context.title_hint)
                        and looks_like_person_name(name)):
                    return {'author': name, 'source': 'text_instructor_label',
                            'page': 1, 'evidence': ' / '.join(lines[index:index + 2])}
        for index in range(1, min(15, len(lines) - 1)):
            if not re.fullmatch(r'\(?\s*(?:org\.?|organizador(?:a)?)\s*\)?', lines[index + 1], re.I):
                continue
            name = normalize_author_candidate(lines[index])
            if (looks_like_person_name(name)
                    and _work_title_near(lines[max(0, index - 5):index], context.title_hint)):
                return {'author': name, 'source': 'text_strict_credit_block',
                        'page': page, 'evidence': ' / '.join(lines[index:index + 2])}
        for index in range(1, min(12, len(lines) - 1)):
            name = normalize_author_candidate(lines[index])
            if not looks_like_person_name(name, title_hint=context.title_hint):
                continue
            if not re.search(r'\b(?:university\s+press|publishing|publishers?|press)\b',
                             ' '.join(lines[index + 1:index + 4]), re.I):
                continue
            if any(re.search(r'\b(?:series editors?|editorial board|translated by)\b', line, re.I)
                   for line in lines[max(0, index - 4):index + 3]):
                continue
            if _work_title_near(lines[max(0, index - 3):index], context.title_hint):
                return {'author': name, 'source': 'text_titlepage_publisher_byline',
                        'page': page, 'evidence': ' / '.join(lines[max(0, index - 3):index + 2])}
    return not_found()
