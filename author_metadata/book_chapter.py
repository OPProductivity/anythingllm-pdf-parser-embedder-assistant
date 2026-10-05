"""Downloaded book contributions: local title/byline versus parent-book credits."""

import re
import unicodedata

from .names import looks_like_person_name, split_author_line_candidates
from .title_evidence import not_found, words

_NUMBER = r'(?:\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)'
_NUMBER_ONLY = re.compile(rf'^{_NUMBER}[.]?$', re.I)
_HEADING = re.compile(rf'^(?:chapter\s+)?{_NUMBER}[.\s]+(.+)$', re.I)
_CHAPTER = re.compile(rf'^chapter(?:\s+{_NUMBER})?$', re.I)
_ROLE = re.compile(r'\b(?:edited by|editors?|translat(?:ed|ors?)|interview(?:ees?| with)|participants?|contributors?|foreword|preface|acknowledg(?:e)?ments?|references|bibliography)\b', re.I)
_FURNITURE = re.compile(r'^(?:doi|https?://|downloaded|ebsco|all rights|copyright|published|online isbn|print isbn|subject:|series:|collection:|keywords?:|abstract|search in|\u00a9)', re.I)
_JOURNAL = re.compile(r'\bISSN\b|\bjournal homepage\b|\bTo cite this article\b|\bVol\.?\s*\d+.*\b(?:Issue|No\.)\b', re.I)


def _lines(text):
    return [' '.join(unicodedata.normalize('NFKC', line).replace('\u00ad', '').split())
            for line in text.splitlines() if line.strip()]


def _opening(samples):
    # A part divider or blank scan may precede the first contribution. A book
    # cover is not such a divider and must not expose its later chapters here.
    for sample in samples or ():
        if not 1 <= int(sample.get('page') or 0) <= 3:
            continue
        lines = _lines(str(sample.get('text') or ''))
        yield sample, lines
        if lines and not (len(' '.join(lines).split()) < 15 and re.match(r'^Part\s+[IVX\d]+$', lines[0], re.I)):
            break


def _names(line):
    if _ROLE.search(line) or _FURNITURE.match(line) or any(mark in line for mark in (':', '?', '!', '(', ')')):
        return []
    raw = line.lstrip("'\u2018\u2019").strip()
    if raw.islower():
        raw = ' '.join(part.capitalize() for part in raw.split())
    names = split_author_line_candidates(raw, require_complete=True)
    return names or ([raw] if looks_like_person_name(raw) else [])


def _title(lines):
    value = ' '.join(lines).strip(' :')
    return (value if 2 <= len(words(value)) <= 28
            and not any(_ROLE.search(line) or _FURNITURE.match(line) for line in lines)
            and not re.search(r'[.!?]\s+[A-Z]', value) else '')


def _numbered_credits(lines):
    for index, line in enumerate(lines[:80]):
        heading = _HEADING.fullmatch(line)
        number_only = _NUMBER_ONLY.fullmatch(line) or _CHAPTER.fullmatch(line)
        if not heading and not number_only:
            continue
        # Export order can place the chapter number between title and byline.
        if number_only and line.isalpha() and not _CHAPTER.fullmatch(line) and index and index + 1 < len(lines):
            names = _names(lines[index + 1])
            for width in range(1, min(index, 4) + 1):
                title = _title(lines[index - width:index])
                if names and title and not _NUMBER_ONLY.fullmatch(lines[index - 1]):
                    yield names, title, index + 1
        # Standard numbered heading, title continuation, then chapter byline.
        for author_index in range(index + 1, min(index + 6, len(lines))):
            names = _names(lines[author_index])
            # A title continuation may have person-like capitalization. The
            # chapter byline is the last compact name line before body text.
            if author_index + 1 < len(lines) and _names(lines[author_index + 1]):
                continue
            title_lines = ([heading.group(1)] if heading else []) + lines[index + 1:author_index]
            title = _title(title_lines)
            if names and title and not any(_NUMBER_ONLY.fullmatch(value) for value in title_lines):
                yield names, title, author_index
        # Some exports emit a complete title/byline before the chapter number.
        if (number_only and index >= 2 and index + 1 < len(lines)
                and re.match(r'^\d{4}[.]?\s+\S', lines[index + 1])):
            names = _names(lines[index - 1])
            title = _title(lines[index - 2:index - 1])
            if names and title:
                yield names, title, index - 1


def chapter_cues(samples):
    for sample, lines in _opening(samples):
        text = '\n'.join(lines)
        if _JOURNAL.search(text[:1800]):
            continue
        chapter_export = bool(re.search(r'/books/(?:edited-volume/)?chapter-pdf/|/edited-volume/\d+/chapter/|\b10\.\d{4,9}/\S*978\d{10}(?:[-_]\d+|\.ch\d+)\b', text, re.I))
        book_export = 'ebook collection' in text.casefold() or 'portion of the ebook' in text.casefold()
        explicit_chapter = any(_CHAPTER.fullmatch(line) for line in lines[:14])
        credits = list(_numbered_credits(lines))
        if chapter_export or (explicit_chapter and (book_export or credits or 'isbn' in text.casefold())):
            return ('chapter_export_or_label',)
        opener = any((_NUMBER_ONLY.fullmatch(line) or _HEADING.fullmatch(line)) and
                     all(_NUMBER_ONLY.fullmatch(previous) or re.match(r'^(?:Template:|Date:|Dir:)', previous)
                         for previous in lines[:index])
                     for index, line in enumerate(lines[:6]))
        if credits and (book_export or opener):
            return ('numbered_chapter_title_byline',)
    return ()


def infer(context):
    if context.profile.kind != 'book_chapter':
        return not_found()
    for sample, lines in _opening(context.samples):
        # Book-platform chapter covers can extract their title after the
        # abstract keyword furniture and parent-volume editorial board.
        if (any(_CHAPTER.fullmatch(line) for line in lines[:14])
                and '/chapter/' in '\n'.join(lines)):
            for index in range(1, min(30, len(lines) - 1)):
                names = _names(lines[index])
                if not names or not re.match(r'^This (?:essay|chapter)\b', lines[index + 1]):
                    continue
                for width in range(1, min(4, index) + 1):
                    title = _title(lines[index - width:index])
                    if title and all(looks_like_person_name(name, title_hint=title) for name in names):
                        return {'author': ', '.join(names), 'source': 'text_strict_credit_block',
                                'page': int(sample['page']), 'evidence': title + ' / ' + lines[index]}
        for names, title, index in _numbered_credits(lines):
            if any(_ROLE.search(line) for line in lines[max(0, index - 6):index]):
                continue
            if not all(looks_like_person_name(name, title_hint=title) for name in names):
                continue
            return {'author': ', '.join(names), 'source': 'text_strict_credit_block',
                    'page': int(sample['page']), 'evidence': title + ' / ' + lines[index]}
    return {'author': '', 'source': 'chapter_roles_not_resolved', 'page': 0,
            'evidence': 'No complete local chapter title/byline; parent-book credits are not chapter authors.'}
