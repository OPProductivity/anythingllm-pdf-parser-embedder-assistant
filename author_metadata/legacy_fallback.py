"""Conservative fallback for unclassified documents; category routes live separately."""

import math
import re
import unicodedata
import urllib.parse
from pathlib import Path

from rag_pdf_tools import normalize_text
from .constants import (
    AUTHOR_ROLE_HINTS, AUTHOR_BLOCK_STOP_HINTS,
    TRUSTED_AUTHOR_INFERENCE_SOURCES,
)
from .names import (
    normalize_author_candidate, looks_like_person_name, looks_like_non_person_byline,
    author_phrase_is_title_fragment, looks_like_publisher_imprint_line,
    has_author_affiliation_hint, split_author_line_candidates,
    extract_adjacent_person_names, strip_known_extraction_structure_labels,
)
from .filename import infer_author_from_filename, structured_filename_surname_evidence
from .profile import classify_document
from .legacy_book_layout import extract_publisher_backed_name, title_page_matches_book
from .review import is_reviewed_work_citation, looks_like_review_heading, extract_affiliated_review_credit
from .report import extract_prepared_by_credit
from .web_article import extract_dated_opening_byline

def extract_adjacent_affiliated_name_pairs(line, title_hint=""):
    """Recover a compact multi-author line followed by an affiliation/email.

    This handles common scholarly title pages where names are adjacent instead
    of comma-separated (``Jane Doe John Roe ... University {emails}``). It
    requires at least two independently person-shaped name pairs before a
    compact affiliation/email marker, so ordinary prose is not promoted.
    """
    raw = normalize_text(line or "")
    if not raw:
        return []
    # Citation furniture must be rejected before tokenization removes its
    # digits and turns "Vol. 50 No. 3" into a person-shaped "Vol. No.".
    if re.search(r"\b(?:vol\.?\s*\d|pp\.?\s*\d|doi\s*:)", raw, flags=re.I):
        return []
    # A biographical role sentence is not a two-author byline.  Without this
    # narrow branch, ``Kristin Thompson is Honorary Fellow at the University``
    # was tokenized as the two plausible-looking pairs ``Kristin Thompson``
    # and ``Honorary Fellow``.  Require both the role grammar and a real
    # affiliation tail before accepting the single name; ordinary prose and
    # genuine adjacent multi-author lines continue through the existing path.
    role_match = re.match(
        r"^(.{3,80}?)\s+is\s+(?:an?\s+)?"
        r"(?:(?:honorary|senior|associate|assistant|visiting|research)\s+)?"
        r"(?:fellow|professor|lecturer|researcher|scholar|faculty member)\s+"
        r"(?:at|with)\s+(.+)$",
        raw,
        flags=re.I,
    )
    if role_match and has_author_affiliation_hint(role_match.group(2), max_chars=180):
        candidate = normalize_author_candidate(role_match.group(1))
        if looks_like_person_name(candidate, title_hint=title_hint, allow_all_caps=True):
            return [candidate]
    email_or_affiliation = bool(re.search(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", raw)) or has_author_affiliation_hint(raw)
    if not email_or_affiliation:
        return []
    match = re.search(
        r"\b(?:university|institute|school|department|college|laboratory|lab|research|google|facebook|microsoft|openai|anthropic|amazon|meta)\b|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}",
        raw,
        flags=re.I,
    )
    prefix = raw[: match.start()] if match else raw.split("{")[0]
    tokens = re.findall(r"[^\W\d_](?:[^\W\d_]|['\u2019.-])*", prefix)
    if any(not token[0].isupper() for token in tokens):
        return []
    if len(tokens) < 4 or len(tokens) % 2:
        return []
    candidates = []
    for index in range(0, len(tokens), 2):
        candidate = normalize_author_candidate(" ".join(tokens[index:index + 2]))
        if not looks_like_person_name(candidate, title_hint=title_hint, allow_all_caps=True):
            return []
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates if len(candidates) >= 2 else []


def extract_explicit_multi_author_byline(line, title_hint=""):
    """Return a compact comma-and title-page byline embedded in a longer line.

    OCR and ebook title pages often flatten the title, subtitle, authors, and
    place of publication onto one line.  Requiring the exact ``Name, Name and
    Name`` grammar keeps this stronger than a later biographical role sentence
    while avoiding arbitrary capitalized-name harvesting from prose.
    """
    raw = normalize_text(line or "")
    if not raw:
        return []
    token = r"[^\W\d_](?:[^\W\d_]|['\u2019.-])+"
    name = rf"{token}\s+{token}"
    match = re.search(
        rf"(?<![\w'\u2019.-])({name})\s*,\s*({name})\s+(?:and|&)\s+({name})(?![\w'\u2019.-])",
        raw,
    )
    if not match:
        return []
    if re.search(r"(?:[,;&]|\band)\s*$", raw[:match.start()], flags=re.I) or re.match(
        r"\s*(?:[,;&]|\band\b)", raw[match.end():], flags=re.I
    ):
        return []
    candidates = [normalize_author_candidate(value) for value in match.groups()]
    if not all(
        looks_like_person_name(candidate, title_hint=title_hint, allow_all_caps=True)
        for candidate in candidates
    ):
        return []
    return candidates


def extract_title_window_adjacent_byline(lines, title_hint=""):
    """Recover a person immediately beside a filename-corroborated title.

    Scholarly title pages frequently wrap the title over two or three lines,
    so the older single-line title match missed an otherwise unambiguous
    byline.  This helper deliberately requires a compact adjacent title window
    to cover most of the meaningful selected-title tokens.  It does not scan
    arbitrary names from the page and it accepts no distant candidate.
    """
    values = [normalize_text(line) for line in (lines or []) if normalize_text(line)]
    if not values or not title_hint:
        return []

    ignored_title_words = {
        "a", "an", "and", "article", "chapter", "ch", "for", "from", "in",
        "of", "on", "paper", "part", "section", "the", "to", "with",
    }

    def content_tokens(value):
        return [
            token.casefold()
            for token in re.findall(r"[^\W\d_]+", normalize_text(value), flags=re.UNICODE)
            if token.casefold() not in ignored_title_words
        ]

    title_tokens = content_tokens(title_hint)
    if len(title_tokens) < 4:
        return []

    def candidate_names(value):
        names = split_author_line_candidates(
            value,
            title_hint=title_hint,
            allow_all_caps=True,
        )
        if names:
            return names
        candidate = normalize_author_candidate(value)
        if looks_like_person_name(candidate, title_hint=title_hint, allow_all_caps=True):
            return [candidate]
        return []

    def title_window_matches(window, names):
        if any(
            re.match(
                r"^(?:abstract|chapter|collection|downloaded from|keywords?|online isbn|"
                r"print isbn|published|search in|series|subject)\b",
                normalize_text(value),
                flags=re.I,
            )
            for value in window
        ):
            return False
        window_tokens = set(content_tokens(" ".join(window)))
        if not window_tokens:
            return False
        # A catalog filename often begins with the author's surname. Remove
        # only that independently corroborated leading token from the expected
        # title; all actual title words still have to be present on the page.
        expected = list(title_tokens)
        surnames = {
            re.findall(r"[^\W\d_]+", name, flags=re.UNICODE)[-1].casefold()
            for name in names
            if re.findall(r"[^\W\d_]+", name, flags=re.UNICODE)
        }
        leading_title_token_matches_surname = bool(expected and expected[0] in surnames)
        all_caps_name_block = all(
            name == name.upper()
            and re.fullmatch(r"[A-ZÀ-ÖØ-Þ][A-ZÀ-ÖØ-Þ'.-]*(?:\s+[A-ZÀ-ÖØ-Þ][A-ZÀ-ÖØ-Þ'.-]*){1,3}", name)
            and not re.search(r"\b(?:EDIT(?:ED)?|EDITORIAL|HISTORY|RESEARCH|STUDIES)\b", name, flags=re.I)
            for name in names
        )
        # A title-case candidate is accepted only when its surname is the
        # catalog prefix of the selected title/filename. Without that second
        # signal, nearby subject headings such as ``Celebrity Studies`` can
        # look person-shaped. An all-caps two-to-four-token name block may
        # stand alone, but then the adjacent page title must match almost
        # exactly.
        if not leading_title_token_matches_surname and not all_caps_name_block:
            return False
        if leading_title_token_matches_surname:
            expected = expected[1:]
        if len(expected) < 4:
            return False
        matched = sum(token in window_tokens for token in expected)
        required_fraction = 0.65 if leading_title_token_matches_surname else 0.85
        return matched >= max(4, math.ceil(len(expected) * required_fraction))

    for index, line in enumerate(values[:24]):
        names = candidate_names(line)
        if not names:
            continue
        for width in range(1, 5):
            previous = values[max(0, index - width):index]
            following = values[index + 1:index + 1 + width]
            if previous and title_window_matches(previous, names):
                return names
            if following and title_window_matches(following, names):
                return names
    return []


def extract_stacked_affiliated_names(lines, title_hint=""):
    """Recover title-page names stacked above affiliation/email lines.

    Scholarly PDFs commonly render each author on one line and put their
    shared affiliation/email block immediately below. Accept a run of two or
    more person-shaped lines before that block, or a single name only when a
    direct email follows. This is deliberately constrained to the opening
    title block supplied by the caller.
    """
    values = [normalize_text(line) for line in (lines or []) if normalize_text(line)]
    recovered = []
    index = 0
    while index < len(values):
        line = values[index]
        if has_author_affiliation_hint(line) or "@" in line:
            index += 1
            continue
        candidate = normalize_author_candidate(line)
        if not looks_like_person_name(candidate, title_hint=title_hint, allow_all_caps=False):
            index += 1
            continue
        group = [candidate]
        cursor = index + 1
        while cursor < len(values):
            next_line = values[cursor]
            if has_author_affiliation_hint(next_line) or "@" in next_line:
                break
            next_candidate = normalize_author_candidate(next_line)
            if not looks_like_person_name(next_candidate, title_hint=title_hint, allow_all_caps=False):
                break
            group.append(next_candidate)
            cursor += 1
        # A wrapped title can end with a connector (for example ``... for``)
        # immediately above its final title words. If that phrase is then
        # followed by a real multi-author block, do not make the wrapped
        # title tail its first author. Restrict this correction to a multi-
        # name group; a single author directly below a long title remains a
        # valid common layout.
        previous = values[index - 1] if index else ""
        if (
            len(group) >= 2
            and (
                re.search(r"\b(?:for|of|and|the|in|on|to|with|from)\s*$", previous, flags=re.I)
                or previous.rstrip().endswith(("-", "‐", "‑", "‒", "–", "—"))
            )
        ):
            group = group[1:]
        context = values[cursor: cursor + 3]
        context_has_email = any("@" in value for value in context)
        context_has_affiliation = any(
            has_author_affiliation_hint(value)
            and not looks_like_publisher_imprint_line(value)
            for value in context
        )
        # A city/country line can be person-shaped after PDF column ordering
        # (for example ``Coventry, UK`` -> ``UK Coventry``).  A following
        # email proves the real author's identity only when the candidate's
        # surname is also represented in that address.  This keeps the useful
        # single-name-plus-email layout without promoting a postal line.
        if len(group) == 1 and context_has_email:
            email_local_parts = [
                match.group(1)
                for value in context
                for match in re.finditer(r"\b([\w.+-]+)@[\w.-]+\.[A-Za-z]{2,}\b", value)
            ]
            folded_name_tokens = [
                "".join(
                    character for character in unicodedata.normalize("NFKD", token).casefold()
                    if character.isascii() and character.isalnum()
                )
                for token in re.findall(r"[^\W\d_][\w'.-]*", group[0], flags=re.UNICODE)
            ]
            folded_name_tokens = [token for token in folded_name_tokens if len(token) >= 3]
            folded_locals = [
                "".join(
                    character for character in unicodedata.normalize("NFKD", local).casefold()
                    if character.isascii() and character.isalnum()
                )
                for local in email_local_parts
            ]
            context_has_email = bool(
                folded_name_tokens
                and any(
                    token in local
                    for token in folded_name_tokens
                    for local in folded_locals
                )
            )
        if context_has_email or (context_has_affiliation and len(group) >= 2):
            for name in group:
                if name not in recovered:
                    recovered.append(name)
            index = max(cursor, index + 1)
        else:
            index += 1
    return recovered


def is_high_confidence_all_caps_titlepage_author(candidate, following_line, title_hint=""):
    """Accept an ambiguous all-caps title-page name only with two signals.

    A publisher line by itself does not establish that the line above it is a
    person: book titles and subtitles commonly occupy exactly that position.
    Outside an explicit ``By``/``Author`` label, require both an immediate
    imprint and a separated one-letter middle initial. This is intentionally
    precision-first. A bare ``JOHN DOE`` title page may remain unresolved,
    whereas a phrase such as ``TWENTIETH CENTURY`` can never become metadata.
    """
    normalized = normalize_author_candidate(candidate)
    words = re.findall(r"[A-Za-z][A-Za-z'.-]*", normalized)
    has_middle_initial = any(re.fullmatch(r"[A-Z]\.", word) for word in words)
    return bool(
        3 <= len(words) <= 4
        and has_middle_initial
        and looks_like_person_name(normalized, title_hint=title_hint, allow_all_caps=True)
        and looks_like_publisher_imprint_line(following_line)
    )


def extract_opening_title_block_byline(lines, title_hint=""):
    """Recover a visible author credit without relying on PDF properties.

    The evidence is deliberately role-sensitive: candidates must be followed
    by article/chapter body, citation, abstract, or affiliation evidence.
    Series/editor furniture is excluded. A lone person adjacent to a matched
    title is handled separately by the title-aware inference path.
    """
    values = [normalize_text(line) for line in (lines or []) if normalize_text(line)]

    def split_inline_affiliation(value):
        match = re.match(r"^(.{3,100}?),\s*(.+)$", value)
        if not match:
            return value, False
        tail = match.group(2)
        is_affiliation = (
            has_author_affiliation_hint(tail, max_chars=180)
            or bool(re.search(r"\b(?:college|institute|state|tech|university)\b", tail, flags=re.I))
        )
        return (match.group(1), True) if is_affiliation else (value, False)

    for index, line in enumerate(values[:16]):
        previous = values[max(0, index - 4):index]
        if any(
            re.search(r"\b(?:series|volume)\s+editors?\b|^editors?$", value, flags=re.I)
            for value in previous
        ):
            continue
        visible_pipe_credit = False
        candidate_line = line
        if "|" in line:
            possible_credit, _separator, possible_title = line.partition("|")
            possible_credit = normalize_author_candidate(possible_credit)
            if (
                len(possible_title.split()) >= 4
                and not re.search(
                    r"\b(?:journal|press|project|review|studies|university)\b",
                    possible_credit,
                    flags=re.I,
                )
                and looks_like_person_name(possible_credit, title_hint="", allow_all_caps=False)
            ):
                candidate_line = possible_credit
                visible_pipe_credit = True
        candidate_line, inline_affiliation = split_inline_affiliation(candidate_line)
        if "," in line and not inline_affiliation:
            continue
        candidate_line = re.sub(
            r"^(?:Dr|Mr|Mrs|Ms|Prof(?:essor)?)\.?\s+",
            "",
            candidate_line,
            flags=re.I,
        )
        candidates = split_author_line_candidates(candidate_line, title_hint="", allow_all_caps=True)
        if not candidates:
            candidate = normalize_author_candidate(candidate_line)
            if looks_like_person_name(candidate, title_hint="", allow_all_caps=True):
                candidates = [candidate]
        if not candidates:
            continue
        normalized_title = normalize_text(title_hint).casefold()
        if normalized_title and any(
            normalize_text(candidate).casefold() == normalized_title
            for candidate in candidates
        ):
            continue
        if any(
            re.search(
                r"\b(?:americans|article|audiences|cinema|communities|critique|editors?|immigrants|"
                r"journal|keyword|latinos|open|press|profile|project|report|research|review|"
                r"students|studies|quarterly|university|women|workers)\b",
                candidate,
                flags=re.I,
            )
            for candidate in candidates
        ):
            continue
        if any(mark in line for mark in ("?", "“", "”", '"')):
            continue
        following_start = index + 1
        if inline_affiliation:
            # Multi-author journal title pages commonly put one
            # ``Name, University`` credit on each consecutive line.
            for extra_line in values[index + 1:min(len(values), index + 5)]:
                extra_candidate_line, extra_is_affiliation = split_inline_affiliation(extra_line)
                if not extra_is_affiliation:
                    break
                extra_candidate_line = re.sub(
                    r"^(?:Dr|Mr|Mrs|Ms|Prof(?:essor)?)\.?\s+",
                    "",
                    extra_candidate_line,
                    flags=re.I,
                )
                extra_candidate = normalize_author_candidate(extra_candidate_line)
                if not looks_like_person_name(extra_candidate, title_hint="", allow_all_caps=True):
                    break
                if extra_candidate not in candidates:
                    candidates.append(extra_candidate)
                following_start += 1
        following = values[following_start:following_start + 3]
        if any(
            re.search(r"\b(?:series|volume)\s+editors?\b|^editors?$", value, flags=re.I)
            for value in following[:1]
        ):
            continue
        if following:
            immediate = normalize_author_candidate(following[0])
            if (
                looks_like_person_name(immediate, title_hint="", allow_all_caps=True)
                or split_author_line_candidates(following[0], title_hint="", allow_all_caps=True)
            ):
                # A title fragment that happens to look like a person often
                # sits immediately above the real byline.
                continue
        context = " ".join(following)
        context_proves_role = bool(
            visible_pipe_credit
            or re.match(
                r"^(?:abstract\b|introduction\b|keywords?\b|to cite this\b|"
                r"this (?:article|chapter|essay|paper|book)\b)",
                context,
                flags=re.I,
            )
            or any(has_author_affiliation_hint(value, max_chars=180) for value in following)
        )
        if context_proves_role:
            return candidates
        if inline_affiliation and len(candidates) >= 2 and following and is_reviewed_work_citation(following[0]):
            review_headings = [
                position for position, value in enumerate(values[:index])
                if looks_like_review_heading(value)
            ]
            if review_headings:
                heading_index = review_headings[-1]
                title_lines = values[heading_index + 1:index]
                if (
                    0 <= len(title_lines) <= 3
                    and index - heading_index <= 5
                    and (
                        len(" ".join(title_lines).split()) >= 4
                        or len(values[heading_index].split()) >= 5
                    )
                    and not any(re.fullmatch(
                        r"(?:interviewees?|participants?|contributors?|editors?|reviewers?)\s*:?",
                        value, flags=re.I,
                    ) for value in title_lines)
                ):
                    return candidates
    return []


def infer_author_from_text_samples(samples, title_hint=""):
    """Resolve visible credits without confusing endorsements or translators."""
    samples = list(samples or [])
    opening = [s for s in samples if 1 <= int(s.get("page") or 0) <= 4]
    # A title-page editor credit is a document role, unlike a quoted review.
    # Require the actual title on the same page; never promote series editors.
    title_key = " ".join(re.findall(r"\w+", str(title_hint).casefold()))
    for sample in opening:
        text = str(sample.get("text") or "")
        lines = [normalize_text(x) for x in text.splitlines() if normalize_text(x)]
        page_key = " ".join(re.findall(r"\w+", text.casefold()))
        if len(title_key.split()) >= 3 and len(title_key) >= 16 and title_key in page_key:
            for i, line in enumerate(lines[:12]):
                if line.casefold() != "editors" or i == 0:
                    continue
                names = split_author_line_candidates(lines[i-1].replace("•", " and "), allow_all_caps=True)
                if names:
                    return {"author": ", ".join(names), "source": "text_edited_by",
                            "page": sample["page"], "evidence": lines[i-1] + " / Editors"}
    filtered = []
    for sample in samples:
        text = str(sample.get("text") or "")
        # Multiple dash-attributed endorsements are not an author title block.
        # Leave ordinary name/affiliation pages and a single quoted passage alone.
        endorsements = re.findall(r"(?m)^\s*[—–]\s*[^\n,]{3,80},", text)
        if len(endorsements) >= 2 and any(c in text for c in '“”"'):
            continue
        filtered.append(sample)
    report = _infer_author_from_text_samples(filtered, title_hint=title_hint)
    return exclude_explicit_translators(report, opening)


def exclude_explicit_translators(report, samples):
    translators = []
    for sample in samples:
        if not 1 <= int(sample.get("page") or 0) <= 4:
            continue
        for name in re.findall(r"(?im)^\s*translated\s+by\s+([^\n]{3,80})", str(sample.get("text") or "")):
            translators.extend(normalize_author_candidate(n) for n in re.split(r"\s+and\s+|[;,]", name, flags=re.I))
    if report.get("author") and translators:
        def name_key(value):
            return unicodedata.normalize("NFC", normalize_author_candidate(value)).casefold()

        excluded = {name_key(n) for n in translators}
        names = split_author_line_candidates(report["author"], allow_all_caps=True) or [
            normalize_author_candidate(report["author"])
        ]
        retained = [n for n in names if name_key(n) not in excluded]
        if names and retained != names:
            report = {**report, "author": ", ".join(retained),
                      "excluded_translator_names": translators}
    return report


def _infer_author_from_text_samples(samples, title_hint=""):
    title_hint = unicodedata.normalize("NFC", title_hint or "")
    document_profile = classify_document(samples, title_hint=title_hint)
    # The length bound is on a complete credit line. A bounded prefix could
    # otherwise be published as a shorter surname when the line was longer.
    footnote_tail = r"[^\S\n]*(?:[*\u2020\u2021\u00a7\u00b6\u2217\u00b9\u00b2\u00b3\u2070-\u2079]{1,3}|[0-9]{1,2})?[^\S\n]*(?:\n|$)"
    credit_letter = r"[^\W\d_\u00b9\u00b2\u00b3\u2070-\u2079]"
    person_credit = rf"{credit_letter}(?:{credit_letter}|[.,'\u2019\- &]){{3,512}}(?={footnote_tail})"
    people_credit = person_credit
    patterns = [
        (rf"(?:^|\n)\s*by\s+({person_credit})", "text_byline"),
        (rf"(?:^|\n)\s*written by\s+({person_credit})", "text_written_by"),
        (rf"(?:^|\n)\s*edited by\s+({person_credit})", "text_edited_by"),
        (rf"(?:^|\n)\s*review(?:ed)?\s+by\s*[:\-]?\s*({person_credit})", "text_review_byline"),
        (rf"(?:^|\n)\s*(?:text|article|essay)\s+by\s+({person_credit})", "text_byline"),
        (rf"(?:^|\n)\s*(?:column|commentary|analysis|opinion)\s+by\s+({person_credit})", "text_column_byline"),
        (rf"(?:^|\n)\s*author(?:\(s\))?\s*[:\-]\s*({people_credit})", "text_author_label"),
        (rf"(?:^|\n)\s*authors?(?:\(s\))?\s*[:\-]\s*({people_credit})", "text_author_label"),
        (rf"(?:^|\n)\s*writers?\s*[:\-]\s*({people_credit})", "text_writer_label"),
        (r"(?:^|\n)\s*(?:book|chapter|article|document)\s+author\s*[:\-]\s*([^\n]{3,512})(?=\n|$)", "text_bibliographic_author_label"),
        (r"(?:^|\n)\s*instructor\s*[:\-]\s*([^\n]{3,512})(?=\n|$)", "text_instructor_label"),
    ]
    weak_fallback = None
    for sample in samples:
        raw_text = strip_known_extraction_structure_labels(
            str(sample.get("text") or "").replace("\r\n", "\n").replace("\r", "\n")
        )
        raw_text = unicodedata.normalize("NFC", raw_text)
        text = normalize_text(raw_text)
        page = int(sample.get("page") or 0)
        if not text:
            continue
        lines = [normalize_text(line) for line in raw_text.splitlines() if normalize_text(line)]
        # Browser print/export workflows can put the local computer user's
        # name in PDF Author metadata while preserving the real web-page
        # credit in both the PDF title and the visible page header.  Accept a
        # leading ``Person | Article title`` credit only when the complete
        # metadata title is independently visible on an opening page.  This
        # is stronger than merely distrusting Safari/Quartz metadata and also
        # works for equivalent exporters without maintaining a producer list.
        if 1 <= page <= 3 and "|" in normalize_text(title_hint):
            title_credit, _separator, title_body = normalize_text(title_hint).partition("|")
            title_credit = normalize_author_candidate(title_credit)
            visible_title = normalize_text(title_hint).casefold()
            visible_lines = [line.casefold() for line in lines[:16]]
            if (
                title_body.strip()
                and 2 <= len(title_credit.split()) <= 5
                and not re.search(
                    r"\b(?:journal|press|project|review|studies|university)\b",
                    title_credit,
                    flags=re.I,
                )
                and looks_like_person_name(title_credit, title_hint="", allow_all_caps=False)
                and any(
                    line == visible_title or line.startswith(visible_title)
                    for line in visible_lines
                )
            ):
                return {
                    "author": title_credit,
                    "source": "text_visible_title_person_prefix",
                    "page": page,
                    "evidence": normalize_text(title_hint),
                }
        # Journal PDFs often carry stale workstation-owner metadata. Prefer a
        # visible byline only when its syntax is unusually strong: a personal
        # name followed by an affiliation, or a bare name immediately echoed
        # by a bibliographic ``From <name>, ...`` line. This avoids promoting
        # ordinary title-case headings to authors.
        affiliated_names: list[str] = []
        # Structural title-page inference is intentionally limited to the
        # opening pages.  References and bibliographies contain the same
        # name/affiliation/publisher shapes, but they are not document
        # bylines. Explicit labelled credits below remain eligible on every
        # sampled page.
        title_page_lines = lines if 1 <= page <= 3 else []
        review_names, review_evidence = extract_affiliated_review_credit(title_page_lines)
        if review_names:
            return {
                "author": ", ".join(review_names),
                "source": "text_review_byline",
                "page": page,
                "evidence": review_evidence,
            }
        opening_declares_series_editors = any(
            re.search(r"\b(?:series|volume)\s+editors?\b|^editors?$", line, flags=re.I)
            for line in title_page_lines[:12]
        )
        opening_declares_explicit_credit = any(
            re.fullmatch(r"(?:by|written\s+by|edited\s+by)\s*:?", line, flags=re.I)
            for line in title_page_lines[:32]
        )
        adjacent_title_names = extract_title_window_adjacent_byline(
            [] if opening_declares_series_editors else title_page_lines,
            title_hint=title_hint,
        )
        if adjacent_title_names:
            return {
                "author": ", ".join(adjacent_title_names[:12]),
                "source": "text_title_window_adjacent_byline",
                "page": page,
                "evidence": " / ".join(adjacent_title_names[:4]),
            }
        # A literal multi-author title-page byline outranks a later author
        # biography.  This must run before the role-affiliation recovery below:
        # otherwise ``Kristin Thompson is Honorary Fellow ...`` can be true
        # evidence for one contributor yet still truncate a three-author book.
        for line in ([] if opening_declares_series_editors else title_page_lines[:16]):
            explicit_names = extract_explicit_multi_author_byline(
                line,
                title_hint=title_hint,
            )
            if explicit_names:
                return {
                    "author": ", ".join(explicit_names),
                    "source": "text_explicit_multi_author_byline",
                    "page": page,
                    "evidence": " / ".join(explicit_names),
                }
        for line in ([] if opening_declares_series_editors else title_page_lines[:24]):
            candidate = ""
            comma_match = re.match(r"^(.{3,80}?),\s*(.+)$", line)
            if (
                comma_match
                # A numeric volume/issue immediately after the comma proves
                # this is a journal masthead (``Film History, 28.3``), not a
                # person followed by an academic affiliation.
                and not re.match(r"^.{3,80}?,\s*\d+(?:\.\d+)?(?:\D|$)", line)
                and has_author_affiliation_hint(comma_match.group(2), max_chars=180)
                and len(comma_match.group(2).split()) <= 18
            ):
                candidate = comma_match.group(1)
            parenthetical_match = re.match(r"^(.{3,80}?)\s*\(([^)]{3,100})\)\s*$", line)
            if parenthetical_match and has_author_affiliation_hint(parenthetical_match.group(2), max_chars=120):
                candidate = parenthetical_match.group(1)
            candidate = normalize_author_candidate(candidate)
            candidate = re.sub(
                r"^(?:Dr|Mr|Mrs|Ms|Prof(?:essor)?)\.?\s+",
                "",
                candidate,
                flags=re.I,
            )
            if candidate and looks_like_person_name(candidate, title_hint=title_hint):
                if candidate not in affiliated_names:
                    affiliated_names.append(candidate)
        if affiliated_names:
            return {
                "author": ", ".join(affiliated_names[:12]),
                "source": "text_affiliated_byline",
                "page": page,
                "evidence": " / ".join(affiliated_names[:4]),
            }
        for line in ([] if opening_declares_series_editors else title_page_lines[:16]):
            adjacent_affiliated_names = extract_adjacent_affiliated_name_pairs(
                line,
                title_hint=title_hint,
            )
            if adjacent_affiliated_names:
                return {
                    "author": ", ".join(adjacent_affiliated_names[:12]),
                    "source": "text_adjacent_affiliated_byline",
                    "page": page,
                    "evidence": " / ".join(adjacent_affiliated_names[:4]),
                }
        for index, line in enumerate(lines[:12]):
            candidate = normalize_author_candidate(line)
            if not looks_like_person_name(candidate, title_hint=title_hint):
                continue
            following = lines[index + 1] if index + 1 < len(lines) else ""
            if re.match(rf"^From\s+{re.escape(candidate)}(?:\s|,)", following, flags=re.I):
                return {
                    "author": candidate,
                    "source": "text_bibliographic_byline",
                    "page": page,
                    "evidence": f"{line} / {following}",
                }
        for pattern, source in patterns:
            match = re.search(pattern, raw_text, flags=re.I)
            if not match:
                continue
            if source == "text_byline" and re.match(r"\s*(?:18|19|20)\d{2}\b", raw_text[match.end():]):
                # A wrapped "by Name, 2002" is a cited work, not this byline.
                continue
            if page > 2 and source in {
                "text_byline",
                "text_written_by",
                "text_edited_by",
                "text_review_byline",
                "text_column_byline",
            }:
                # Closing pages are retained for explicit biographies, but a
                # bibliography continuation can also start with ``by Name,
                # New York: Publisher``. A plain late-page byline is therefore
                # insufficient to mutate durable metadata. Labelled role and
                # biography evidence below remains available.
                continue
            raw_credit = match.group(1)
            # A conjunction advertises multiple authors; normalizing the
            # entire comma-and list as one inverted catalog name reorders it.
            if re.search(r"\band\b|&", raw_credit, flags=re.I):
                names = split_author_line_candidates(
                    raw_credit, title_hint=title_hint, require_complete=True,
                )
                if names:
                    return {"author": ", ".join(names[:12]), "source": source,
                            "page": page, "evidence": match.group(0).strip()}
            candidate = normalize_author_candidate(raw_credit)
            if looks_like_person_name(candidate, title_hint=title_hint):
                return {
                    "author": candidate,
                    "source": source,
                    "page": page,
                    "evidence": match.group(0).strip(),
                }
            if looks_like_non_person_byline(candidate):
                # Do not trade an explicit collective credit for a later,
                # lower-confidence title-block match from a related-links or
                # publisher page.  The document still gets its title fallback.
                return {
                    "author": "",
                    "source": "text_non_person_byline",
                    "page": page,
                    "evidence": match.group(0).strip(),
                }
            split_candidates = split_author_line_candidates(candidate, title_hint=title_hint)
            if split_candidates:
                return {
                    "author": ", ".join(split_candidates[:12]),
                    "source": source,
                    "page": page,
                    "evidence": match.group(0).strip(),
                }

        # Explicit ``By``/``Author``/``Instructor`` labels above are more
        # reliable than title-block geometry. Only then consider the compact
        # stacked-name layout used by scholarly title pages.
        stacked_affiliated_names = extract_stacked_affiliated_names(
            [] if opening_declares_series_editors else title_page_lines[:40],
            title_hint=title_hint,
        )
        if stacked_affiliated_names:
            return {
                "author": ", ".join(stacked_affiliated_names[:12]),
                "source": "text_stacked_affiliated_byline",
                "page": page,
                "evidence": " / ".join(stacked_affiliated_names[:4]),
            }

        top_lines = lines[:18]
        # Title pages frequently place a single ordinary-cased author line
        # immediately beside a publisher imprint. That is strong enough to
        # accept without mistaking preceding title fragments for a byline.
        # Later book title pages need both book-front-matter evidence and a
        # same-page title match; other layouts retain the page-one boundary.
        later_book_title_page = (
            document_profile.kind == "book"
            and page in (2, 3)
            and title_page_matches_book(top_lines, title_hint)
        )
        if later_book_title_page and not opening_declares_series_editors and not opening_declares_explicit_credit:
            name, evidence = extract_publisher_backed_name(
                top_lines, title_hint,
                normalize_author_candidate=normalize_author_candidate,
                looks_like_person_name=looks_like_person_name,
                looks_like_publisher_imprint_line=looks_like_publisher_imprint_line,
                later_book_title_page=True,
            )
            if name:
                return {"author": name, "source": "text_titlepage_publisher_byline",
                        "page": page, "evidence": evidence}
        if (
            page == 1
            and not opening_declares_series_editors
            and not opening_declares_explicit_credit
        ):
            name, evidence = extract_publisher_backed_name(
                top_lines, title_hint,
                normalize_author_candidate=normalize_author_candidate,
                looks_like_person_name=looks_like_person_name,
                looks_like_publisher_imprint_line=looks_like_publisher_imprint_line,
            )
            if name:
                return {"author": name, "source": "text_titlepage_publisher_byline",
                        "page": page, "evidence": evidence}
            # Some scanned articles split a compact all-caps byline over the
            # first two visual lines. Only consider that exact position: a
            # wider scan would turn a stacked book title into a person's name.
            stacked_author = None
            if len(top_lines) >= 2:
                stacked = normalize_author_candidate(f"{top_lines[0]} {top_lines[1]}")
                if (
                    re.fullmatch(r"(?:[A-Z][A-Z'.-]*\s+){1,3}[A-Z][A-Z'.-]*", stacked)
                    # The filename/title hint can itself contain an explicit
                    # ``-first-last`` credit. Here the first-two-lines layout
                    # is independent author evidence, so do not reject the
                    # name merely because that same credit also appears in a
                    # machine-generated filename.
                    and looks_like_person_name(stacked, title_hint="", allow_all_caps=True)
                ):
                    stacked_author = {
                        "author": stacked,
                        "source": "text_first_lines_stacked_byline",
                        "page": page,
                        "evidence": f"{top_lines[0]} / {top_lines[1]}",
                    }
            # A one-line all-caps byline is accepted only when followed
            # immediately by an imprint *and* it has a structural personal
            # name cue. A publisher alone is not enough: a title or subtitle
            # can sit directly above an imprint in the same layout position.
            for index, line in enumerate(top_lines[:8]):
                candidate = normalize_author_candidate(line)
                # Require the *immediate* next non-empty line to be the
                # imprint. Looking across two lines lets an all-caps subtitle
                # borrow the real author line's following publisher and turn
                # the subtitle into a false author.
                following = top_lines[index + 1] if index + 1 < len(top_lines) else ""
                if (
                    re.fullmatch(r"(?:[A-Z][A-Z'.-]*\s+){1,3}[A-Z][A-Z'.-]*", candidate)
                    and is_high_confidence_all_caps_titlepage_author(
                        candidate,
                        following,
                        title_hint=title_hint,
                    )
                ):
                    return {
                        "author": candidate,
                        "source": "text_compact_caps_byline",
                        "page": page,
                        "evidence": f"{line} / publication-metadata-nearby",
                    }
            # An unlabelled stacked name is weaker than the explicit
            # initial-plus-imprint evidence above. Do not let a recovered
            # title line short-circuit that stronger author evidence.
            if stacked_author is not None:
                return stacked_author
        title_start_index = 0
        title_matched = False
        normalized_title = (
            normalize_text(re.sub(r"[_\W]+", " ", title_hint, flags=re.UNICODE)).casefold()
            if title_hint else ""
        )
        if normalized_title:
            for index, line in enumerate(top_lines):
                lowered = normalize_text(
                    re.sub(r"[_\W]+", " ", line, flags=re.UNICODE)
                ).casefold()
                if lowered and (lowered in normalized_title or normalized_title in lowered):
                    title_start_index = index
                    title_matched = True
                    break
        # A large and common scholarly layout is simply ``Title`` followed by
        # one or more person names.  The older generic top-block fallback saw
        # this evidence but deliberately left it untrusted, so genuine names
        # such as Laura Mulvey remained Unknown.  Promote only the line
        # immediately following a positively matched title (or a short run of
        # wrapped title lines), never an arbitrary title-shaped line.  A bare
        # one-word marker such as ``Bold`` or ``Edition`` still cannot satisfy
        # the person-name predicate.
        if (
            title_matched
            and 1 <= page <= 3
            and not opening_declares_series_editors
            and not opening_declares_explicit_credit
        ):
            for candidate_index in range(title_start_index + 1, min(len(top_lines), title_start_index + 5)):
                line = top_lines[candidate_index]
                # Some downloaded filenames encode spaces as _20. Decode
                # only for this wrapped-title exclusion, not as author proof.
                decoded_title = urllib.parse.unquote(re.sub(r"_([0-9A-Fa-f]{2})", r"%\1", title_hint))
                if author_phrase_is_title_fragment(line, decoded_title):
                    continue
                # A reviewed work's bibliographic ``Title, by Names`` line is
                # not a byline for the review immediately above it.
                if document_profile.kind == "review" and re.search(r",\s*by\s+", line, flags=re.I):
                    continue
                if line.casefold() in AUTHOR_BLOCK_STOP_HINTS:
                    break
                candidates = split_author_line_candidates(
                    line,
                    title_hint=title_hint,
                    allow_all_caps=False,
                )
                if not candidates:
                    candidate = normalize_author_candidate(line)
                    if looks_like_person_name(candidate, title_hint=title_hint, allow_all_caps=False):
                        candidates = [candidate]
                if candidates:
                    following = top_lines[candidate_index + 1] if candidate_index + 1 < len(top_lines) else ""
                    # Multi-column bibliographic furniture can put a broken
                    # citation name directly after an article title (for
                    # example ``Alary Helen`` / ``Washington, Invented``).
                    # A short comma fragment is evidence of that column merge,
                    # not an author credit. Ordinary prose after a real name
                    # remains allowed even when its sentence contains commas.
                    if "," in following and len(following.split()) <= 5:
                        continue
                    following_candidate = normalize_author_candidate(following)
                    if (
                        len(candidates) == 1
                        and not has_author_affiliation_hint(following, max_chars=180)
                        and (
                            looks_like_person_name(
                                following_candidate,
                                title_hint="",
                                allow_all_caps=True,
                            )
                            or split_author_line_candidates(
                                following,
                                title_hint="",
                                allow_all_caps=True,
                            )
                        )
                    ):
                        # The current line is the last title fragment; the
                        # immediately following person is the actual byline.
                        continue
                    strong_visible_role = bool(
                        len(candidates) >= 2
                        or re.match(
                            r"^(?:abstract\b|introduction\b|keywords?\b|to cite this\b|"
                            r"this (?:article|chapter|essay|paper|book)\b)",
                            following,
                            flags=re.I,
                        )
                        or has_author_affiliation_hint(following, max_chars=180)
                    )
                    return {
                        "author": ", ".join(candidates[:12]),
                        "source": (
                            "text_opening_title_block_byline"
                            if strong_visible_role
                            else "text_title_adjacent_byline"
                        ),
                        "page": page,
                        "evidence": line,
                    }
                # Continue across wrapped title lines, but stop as soon as
                # prose, a heading, or publication furniture begins.
                normalized_candidate_line = normalize_text(
                    re.sub(r"[_\W]+", " ", line, flags=re.UNICODE)
                ).casefold()
                is_wrapped_title_line = bool(
                    normalized_candidate_line
                    and normalized_candidate_line in normalized_title
                )
                if not is_wrapped_title_line and (
                    len(line.split()) > 10 or re.search(r"[.!?]$", line)
                ):
                    break
        # If metadata/filename title matching was unavailable or deliberately
        # abstained, independently inspect the visible opening title block.
        # Keeping this after all established title-aware rules prevents it
        # from displacing their stronger multi-author and publisher grammar.
        if page == 1:
            opening_byline = extract_opening_title_block_byline(
                title_page_lines,
                title_hint=title_hint,
            )
            if opening_byline:
                return {
                    "author": ", ".join(opening_byline[:12]),
                    "source": "text_opening_title_block_byline",
                    "page": page,
                    "evidence": " / ".join(opening_byline[:4]),
                }
        generic_fallback_last_index = min(len(top_lines) - 1, title_start_index + 8)
        for index, line in enumerate(top_lines):
            lowered = line.casefold().strip()
            if (
                lowered not in AUTHOR_ROLE_HINTS
                and not re.fullmatch(r"\d+\s+authors?\s*:?", lowered)
            ):
                continue
            if index + 1 >= len(top_lines):
                continue
            candidate = normalize_author_candidate(top_lines[index + 1])
            if looks_like_person_name(candidate, title_hint=title_hint):
                return {
                    "author": candidate,
                    "source": "text_role_followup",
                    "page": page,
                    "evidence": f"{line} / {top_lines[index + 1]}",
                }
        detected_names = []
        # A bare title-case name is weak evidence.  Keep that fallback on the
        # actual title page (or for documents with no usable title at all),
        # never on later prose pages where organisations and locations often
        # happen to look like names.  Explicit ``By …``/role credits above
        # continue to work on any of the inspected opening pages.
        bibliographic_opening_context = bool(
            re.search(r"\b(?:18|19|20)\d{2}\b", " ".join(top_lines))
            and any(";" in line for line in top_lines)
        )
        allow_generic_top_block = (
            (bool(title_matched) or (not normalized_title and page <= 2))
            and not bibliographic_opening_context
        )
        reviewed_work_context = False
        for index, line in enumerate(top_lines):
            if index < title_start_index:
                continue
            decoded_title = urllib.parse.unquote(re.sub(r"_([0-9A-Fa-f]{2})", r"%\1", title_hint))
            if author_phrase_is_title_fragment(line, decoded_title):
                continue
            lowered = line.casefold().strip()
            if not lowered:
                continue
            if lowered.startswith("reviewed work"):
                # Book-review headers commonly put the reviewed title in the
                # exact visual slot used by an author line.  A later explicit
                # credit can still win, but bare-title fallback must abstain.
                reviewed_work_context = True
                continue
            if lowered in AUTHOR_BLOCK_STOP_HINTS or re.match(r"^\d+\s+introduction\b", lowered):
                break
            if index > generic_fallback_last_index:
                continue
            if "@" in line or has_author_affiliation_hint(line):
                continue
            if not allow_generic_top_block or reviewed_work_context:
                continue
            split_candidates = split_author_line_candidates(
                line,
                title_hint=title_hint,
                allow_all_caps=False,
            )
            if split_candidates:
                for candidate in split_candidates:
                    if candidate not in detected_names:
                        detected_names.append(candidate)
                continue
            adjacent_candidates = extract_adjacent_person_names(
                line,
                title_hint=title_hint,
                allow_all_caps=False,
            )
            if adjacent_candidates:
                for candidate in adjacent_candidates:
                    if candidate not in detected_names:
                        detected_names.append(candidate)
                continue
            candidate = normalize_author_candidate(line)
            if (
                looks_like_person_name(candidate, title_hint=title_hint, allow_all_caps=False)
                and candidate not in detected_names
            ):
                detected_names.append(candidate)
        if detected_names:
            # Keep looking through the bounded opening-page sample. A weak
            # generic name on a download/copyright cover must not prevent a
            # later proper title page from supplying an explicit title-adjacent
            # or publisher-backed author. The weak result remains available
            # for review only if no stronger source appears.
            weak_fallback = weak_fallback or {
                "author": ", ".join(detected_names[:12]),
                "source": "text_top_block_names",
                "page": page,
                "evidence": " / ".join(detected_names[:4]),
            }
    return weak_fallback or {"author": "", "source": "not_found", "page": 0, "evidence": ""}


def infer_author_from_strict_credit_blocks(samples, path: Path, title_hint=""):
    """Recover complete visible credits only after ordinary inference abstains.

    The detector is intentionally structural and opening-page-only.  It does
    not scan arbitrary capitalized names: the complete name block must touch a
    visible match for the independently resolved title, or the article's
    abstract boundary.  This preserves abstention when the available excerpt
    contains only series furniture, citations, or prose names.
    """
    ignored_title_words = {
        "a", "an", "and", "article", "chapter", "for", "from", "in", "of",
        "on", "part", "section", "the", "to", "with",
    }
    blocked_credit_terms = re.compile(
        r"\b(?:corresponding\s+author|editors?\s+emeriti|series\s+editors?|"
        r"volume\s+editors?|editorial\s+board|reviewed\s+by|instructor|"
        r"journal|press|publishers?|series|volume|chapter|section|abstract|keywords?|academia)\b",
        flags=re.I,
    )

    def title_tokens(value):
        return [
            token.casefold()
            for token in re.findall(r"[^\W\d_]+", normalize_text(value), flags=re.UNICODE)
            if token.casefold() not in ignored_title_words
        ]

    def clean_credit(value):
        value = re.sub(r"(?<=\w)[*†‡§¶∗¹²³⁰-⁹]+\b", "", value or "")
        value = re.sub(r"(?<=\w)\d{1,2}(?=\s*(?:,|and\b|&|$))", "", value, flags=re.I)
        value = re.sub(
            r"(?<=\w)\s+[a-d](?:\s*,\s*[a-d]){0,3}\s*[,⁎*]*(?=\s*(?:,|and\b|&|$))",
            "",
            value,
        )
        return normalize_text(value)

    def names_from_block(block):
        raw = " ".join(clean_credit(value) for value in block if clean_credit(value))
        if (
            not raw
            or blocked_credit_terms.search(raw)
            or "•" in raw
            or has_author_affiliation_hint(raw, max_chars=240)
        ):
            return []
        names = split_author_line_candidates(raw, title_hint=title_hint, allow_all_caps=True)
        if not names:
            candidate = normalize_author_candidate(raw)
            if looks_like_person_name(candidate, title_hint=title_hint, allow_all_caps=True):
                names = [candidate]
        if not names:
            return []
        for name in names:
            tokens = re.findall(r"[^\W\d_]+", name, flags=re.UNICODE)
            if not 2 <= len(tokens) <= 6:
                return []
            # Initials must retain their punctuation; otherwise a wrapped
            # title/location fragment with a one-letter token is not a name.
            if any(
                len(token) == 1 and not re.search(rf"\b{re.escape(token)}\.", raw)
                for token in tokens
            ):
                return []
        return names

    def widest_complete_block(values, left, right, *, from_start=False, maximum_lines=3):
        candidates = []
        maximum = min(maximum_lines, max(0, right - left))
        for width in range(1, maximum + 1):
            block = values[left:left + width] if from_start else values[right - width:right]
            if width > 1:
                # Wrapped author lists advertise their continuation. Without
                # this requirement, maximizing name count joins a title tail
                # or an organisation line to an otherwise valid lone author.
                joins = [
                    bool(
                        re.search(r"(?:,|\band\b|&)\s*$", block[index], flags=re.I)
                        or re.match(r"^\s*(?:and\b|&)", block[index + 1], flags=re.I)
                    )
                    for index in range(len(block) - 1)
                ]
                if not all(joins):
                    continue
            names = names_from_block(block)
            if names:
                candidates.append((len(names), width, names, block))
        return max(candidates, default=(0, 0, [], []))

    expected = title_tokens(title_hint)
    for sample in samples or []:
        try:
            page = int(sample.get("page") or 0)
        except (TypeError, ValueError):
            continue
        if not 1 <= page <= 3:
            continue
        raw_text = strip_known_extraction_structure_labels(
            str(sample.get("text") or "").replace("\r\n", "\n").replace("\r", "\n")
        )
        values = [normalize_text(line) for line in raw_text.splitlines() if normalize_text(line)][:64]
        if not values:
            continue

        # Explicit work-level role labels are sufficient on their own. Gather
        # every consecutive name line before returning so ``Edited by`` never
        # truncates a wrapped three-person editor block.
        for index, line in enumerate(values[:40]):
            inline = re.match(r"^(?:by|written\s+by|edited\s+by)\s+(.+)$", line, flags=re.I)
            if inline:
                credit_lines = [inline.group(1), *values[index + 1:index + 6]]
                _, _, names, block = widest_complete_block(
                    credit_lines, 0, len(credit_lines), from_start=True, maximum_lines=6,
                )
                if names:
                    return {
                        "author": ", ".join(names[:12]),
                        "source": "text_strict_credit_block",
                        "page": page,
                        "evidence": " / ".join([line, *block[1:]]),
                    }
            if not re.fullmatch(r"(?:by|written\s+by|edited\s+by)\s*:?", line, flags=re.I):
                continue
            collected = []
            evidence_lines = []
            for candidate_line in values[index + 1:index + 6]:
                names = names_from_block([candidate_line])
                if not names:
                    break
                collected.extend(name for name in names if name not in collected)
                evidence_lines.append(candidate_line)
            if collected:
                return {
                    "author": ", ".join(collected[:12]),
                    "source": "text_strict_credit_block",
                    "page": page,
                    "evidence": f"{line} / {' / '.join(evidence_lines)}",
                }

        matched_window = None
        if len(expected) >= 3:
            # PDF object order can place a visually opening title/byline
            # after a body column. Keep the same 88% title-match requirement,
            # but cover that bounded first-page object-order displacement.
            for start in range(min(56, len(values))):
                for width in range(1, min(7, len(values) - start + 1)):
                    observed = set(title_tokens(" ".join(values[start:start + width])))
                    matched = sum(token in observed for token in expected)
                    candidate = (matched / len(expected), matched, -width, start, start + width)
                    if matched_window is None or candidate > matched_window:
                        matched_window = candidate
        if matched_window and matched_window[0] >= 0.88 and matched_window[1] >= 3:
            _fraction, _matched, _negative_width, start, end = matched_window
            before = widest_complete_block(values, max(0, start - 3), start)
            after = widest_complete_block(values, end, min(len(values), end + 3), from_start=True)
            viable = [after] if after[2] else []
            if before[2]:
                filename_surnames = structured_filename_surname_evidence(Path(path))
                before_surnames = {
                    tokens[-1].casefold()
                    for name in before[2]
                    if (tokens := re.findall(r"[^\W\d_]+", name, flags=re.UNICODE))
                }
                if before_surnames and before_surnames.issubset(filename_surnames):
                    viable.append(before)
            if viable:
                _count, _width, names, block = max(viable, key=lambda item: (item[0], item[1]))
                return {
                    "author": ", ".join(names[:12]),
                    "source": "text_strict_credit_block",
                    "page": page,
                    "evidence": " / ".join(block),
                }

        # A complete name block immediately above Abstract is independently
        # strong. Permit one intervening compact affiliation line, but never
        # a publisher/series/editor label.
        for index, line in enumerate(values[:32]):
            if not re.fullmatch(
                r"(?:abstract|a\s*b\s*s\s*t\s*r\s*a\s*c\s*t)\s*:?,?",
                line,
                flags=re.I,
            ):
                continue
            cursor = index
            if cursor and has_author_affiliation_hint(values[cursor - 1], max_chars=220):
                cursor -= 1
            _count, _width, names, block = widest_complete_block(
                values,
                max(0, cursor - 3),
                cursor,
            )
            if names:
                return {
                    "author": ", ".join(names[:12]),
                    "source": "text_strict_credit_block",
                    "page": page,
                    "evidence": " / ".join(block),
                }
        # Chapter/course-pack layouts may have no useful PDF title metadata.
        # A complete byline is nevertheless strong when it sits between an
        # explicit numbered chapter heading and the opening prose. This is a
        # layout grammar, not a generic search for capitalized names.
        for index in range(1, min(48, len(values) - 1)):
            names = names_from_block([values[index]])
            if not names:
                continue
            previous = values[max(0, index - 4):index]
            following = values[index + 1:min(len(values), index + 3)]
            previous_text = " ".join(previous)
            next_line = following[0] if following else ""
            numbered_heading = bool(
                re.search(r"(?:^|\s)\d{1,3}\s+[A-ZÀ-ÖØ-Þ][^.!?]{5,}$", previous_text)
                or (
                    any(re.fullmatch(r"\d{1,3}", value) for value in previous)
                    and any(len(value.split()) >= 3 for value in previous)
                )
                or any(re.fullmatch(r"i\s*n\s*t\s*r\s*o\s*d\s*u\s*c\s*t\s*i\s*o\s*n", value, flags=re.I) for value in previous)
            )
            opening_text = " ".join(following[:2])
            opening_prose = len(opening_text.split()) >= 10 and bool(re.search(r"[.!?:;]", opening_text))
            if numbered_heading and opening_prose:
                return {
                    "author": ", ".join(names[:12]),
                    "source": "text_strict_credit_block",
                    "page": page,
                    "evidence": f"{previous[-1]} / {values[index]}",
                }
            # Student submissions identify their author with a directly
            # adjacent student number. Instructors elsewhere on the cover are
            # therefore not interchangeable with this credit.
            if (
                re.fullmatch(r"[A-Z]\d{6,10}", next_line, flags=re.I)
                and any(re.search(r"\b(?:essay|assignment|thesis|paper)\b", value, flags=re.I) for value in following[1:3])
            ):
                return {
                    "author": ", ".join(names[:12]),
                    "source": "text_strict_credit_block",
                    "page": page,
                    "evidence": f"{values[index]} / {next_line}",
                }
            # Publisher web exports can put the byline directly below the
            # article title and then state its containing volume explicitly.
            if next_line.casefold().startswith("this article appears in"):
                return {
                    "author": ", ".join(names[:12]),
                    "source": "text_strict_credit_block",
                    "page": page,
                    "evidence": f"{values[index]} / {next_line}",
                }
    return {"author": "", "source": "not_found", "page": 0, "evidence": ""}


def opening_filename_corroborated_credit(samples, path: Path, title_hint=""):
    """A first-page work credit may outrank a later citation's By/Edited by.

    No free search for names: require a complete standalone name in the first
    twelve lines (or an explicit first-page bibliographic/worker credit), plus
    an independently matching surname at the beginning of the filename.
    """
    tokens = re.findall(r"[^\W\d_]+", Path(path).stem.casefold())
    if not tokens:
        return {}
    candidates = {}
    for sample in samples:
        if sample.get("page") != 1:
            continue
        lines = [normalize_text(re.sub(r"[\u202a-\u202e\u2066-\u2069]", "", s))
                 for s in str(sample.get("text") or "").splitlines() if s.strip()]
        for index, line in enumerate(lines[:18]):
            candidate = line if index < 12 else ""
            explicit_byline = index < 12 and bool(re.match(r"^(?:by|written\s+by)\s+", line, re.I))
            bibliography = re.match(r'^([^,]{5,70}),\s*[“"‘]', line)
            reported = re.match(r"^Reported\s+by\s+(.+?)\s*\(\s*Staff\s+Writer\s*\)", line, re.I)
            if bibliography and index == 0:
                candidate = bibliography.group(1)
            elif reported:
                candidate = reported.group(1)
            # A visible By credit corroborated by the filename is not a title
            # fragment merely because the PDF title also includes that name.
            # Bare names retain the existing title-fragment exclusion.
            if not looks_like_person_name(candidate, title_hint="" if explicit_byline else title_hint, allow_all_caps=False):
                continue
            name = normalize_author_candidate(candidate)
            names = re.findall(r"[^\W\d_]+", name.casefold())
            if names and (names[-1] == tokens[0] or tokens[:len(names)] == names):
                candidates[name] = line
    if len(candidates) != 1:
        return {}
    name, evidence = next(iter(candidates.items()))
    return {"author": name, "source": "filename_corroborated_text_name", "page": 1,
            "evidence": evidence + " / opening filename credit"}


def infer_author_from_samples_or_filename(samples, path: Path, title_hint=""):
    """Apply the established sample rules, then the existing filename fallback."""
    report = _infer_author_from_samples_or_filename(samples, path, title_hint=title_hint)
    return exclude_explicit_translators(report, samples)


def _infer_author_from_samples_or_filename(samples, path: Path, title_hint=""):
    decoded_stem = urllib.parse.unquote(re.sub(r"_([0-9A-Fa-f]{2})", r"%\1", Path(path).stem))
    catalog_parts = decoded_stem.split("--")
    if len(catalog_parts) >= 3:
        def name_key(value):
            return " ".join(re.findall(r"[^\W\d_]+", "".join(
                c for c in unicodedata.normalize("NFKD", value).casefold()
                if not unicodedata.combining(c)
            )))
        for sample in samples:
            lines = str(sample.get("text") or "").strip().splitlines()
            first = normalize_author_candidate(lines[0]) if lines else ""
            if sample.get("page") == 1 and looks_like_person_name(first, allow_all_caps=False):
                if any(name_key(first) == name_key(part) for part in catalog_parts[1:]):
                    return {"author": first, "source": "filename_corroborated_text_name", "page": 1,
                            "evidence": first + " / delimited filename credit"}
    report = infer_author_from_text_samples(samples, title_hint=title_hint)
    if report.get("source") == "text_non_person_byline":
        return report
    if (report.get("source") == "text_role_followup"
            and re.match(r"^editors?\s*/", str(report.get("evidence") or ""), flags=re.I)):
        first_page = next((sample for sample in samples if int(sample.get("page") or 0) == 1), None)
        opening_lines = str(first_page.get("text") or "").splitlines()[:40] if first_page else []
        issue_header = any(re.match(
            r"^\s*(?:issue\s+\d+(?:\s*\(\d+\))?|"
            r"volume\s+(?:\d+|one|two|three|four|five)\s*[/|]\s*"
            r"number\s+(?:\d+|one|two|three|four|five))\b",
            line, flags=re.I,
        ) for line in opening_lines)
        if issue_header:
            return {**report, "author": "", "source": "unresolved_issue_editor_role"}
    opening_credit = opening_filename_corroborated_credit(samples, path, title_hint)
    if opening_credit and (not report.get("author") or int(report.get("page") or 0) > 1
                           or report.get("source") not in TRUSTED_AUTHOR_INFERENCE_SOURCES):
        return opening_credit
    if not (report.get("author") and report.get("source") in TRUSTED_AUTHOR_INFERENCE_SOURCES):
        for sample in samples:
            if int(sample.get("page") or 0) != 1:
                continue
            lines = str(sample.get("text") or "").splitlines()
            names = extract_dated_opening_byline(lines, title_hint=title_hint)
            if names:
                evidence = next((normalize_text(line) for line in lines
                                 if "|" in line and normalize_author_candidate(line.partition("|")[0]) == names[0]), "")
                if not evidence:
                    for index in range(len(lines) - 1):
                        if normalize_author_candidate(lines[index]) != names[0]:
                            continue
                        if re.fullmatch(
                            r"[A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?,\s*\d{4}",
                            normalize_text(lines[index + 1]), flags=re.I,
                        ):
                            evidence = " / ".join(normalize_text(value) for value in lines[index:index + 3])
                            break
                return exclude_explicit_translators({
                    "author": names[0], "source": "text_dated_opening_byline",
                    "page": 1, "evidence": evidence,
                }, samples)
    filename_report = infer_author_from_filename(path, title_hint=title_hint)
    if report.get("author") and report.get("source") in TRUSTED_AUTHOR_INFERENCE_SOURCES:
        return report
    if filename_report.get("author") and filename_report.get("source") in TRUSTED_AUTHOR_INFERENCE_SOURCES:
        return filename_report
    document_profile = classify_document(samples, title_hint=title_hint)
    if document_profile.kind == "report":
        for sample in samples:
            page = int(sample.get("page") or 0)
            if not 1 <= page <= 4:
                continue
            name, evidence = extract_prepared_by_credit(
                str(sample.get("text") or "").splitlines(),
            )
            if name:
                return {"author": name, "source": "text_report_prepared_by",
                        "page": page, "evidence": evidence}
    strict_credit = infer_author_from_strict_credit_blocks(samples, Path(path), title_hint=title_hint)
    if strict_credit.get("author"):
        return strict_credit
    # A bare title-page name is too weak on its own, but becomes materially
    # different evidence when the same complete name is independently encoded
    # at the start of the selected filename.  Compare normalized tokens so
    # ``jodi-dean-title.pdf`` can corroborate ``Jodi Dean`` without making a
    # generic first filename word an author.
    if report.get("author"):
        author_tokens = [
            token.casefold()
            for token in re.findall(r"[^\W\d_]+", normalize_author_candidate(report["author"]), flags=re.UNICODE)
        ]
        stem_tokens = [
            token.casefold()
            for token in re.findall(r"[^\W\d_]+", normalize_text(Path(path).stem), flags=re.UNICODE)
        ]
        if len(author_tokens) >= 2 and stem_tokens[:len(author_tokens)] == author_tokens:
            return {
                **report,
                "source": "filename_corroborated_text_name",
                "evidence": f"{report.get('evidence') or ''} / {Path(path).name}".strip(" /"),
            }
        filename_surnames = structured_filename_surname_evidence(Path(path))
        report_names = split_author_line_candidates(
            report.get("author") or "",
            title_hint="",
            allow_all_caps=True,
        ) or [normalize_author_candidate(report.get("author") or "")]
        report_surnames = {
            tokens[-1].casefold()
            for name in report_names
            if (tokens := re.findall(r"[^\W\d_]+", name, flags=re.UNICODE))
        }
        if report_surnames and report_surnames.issubset(filename_surnames):
            return {
                **report,
                "source": "filename_structured_corroborated_text_names",
                "evidence": f"{report.get('evidence') or ''} / {Path(path).name}".strip(" /"),
            }
    return exclude_explicit_translators(report if report.get("author") else filename_report, samples)
