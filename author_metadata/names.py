"""Shared author name normalization and structural validation."""
import re
from rag_pdf_tools import normalize_text
from .constants import (
    AUTHOR_STOP_TERMS, AUTHOR_NAME_PARTICLES, AUTHOR_AFFILIATION_HINTS,
    AUTHOR_ORGANIZATION_TERMS, AUTHOR_NON_PERSON_BYLINE_TERMS, HEADING_STOPWORDS,
)


def looks_like_review_heading(value):
    """Recognize a review-genre heading, not any title mentioning a review."""
    heading = normalize_text(value or "")
    if not heading or len(heading) > 120:
        return False
    return bool(
        re.fullmatch(
            r"(?:(?:book|film|documentary(?:\s+film)?|exhibition|performance|article|media|work)\s+)?"
            r"reviews?(?:\s*[:\-–—]\s*.+)?",
            heading,
            flags=re.I,
        )
        or re.fullmatch(
            r"(?:a\s+)?review\s+essays?(?:\s+(?:on|of)\s+.+|\s*[:\-–—]\s*.+)?",
            heading,
            flags=re.I,
        )
    )


def normalize_author_candidate(value):
    # Directional formatting from PDF exports is not part of a person's name.
    candidate = normalize_text(re.sub(r"[\u202a-\u202e\u2066-\u2069]", "", value or ""))
    candidate = candidate.lstrip("> ")
    candidate = re.sub(r"[*†‡§¶∗]+", " ", candidate)
    candidate = re.sub(
        r"^(?:by|written by|edited by|review(?:ed)? by|text by|article by|essay by|column by|commentary by|analysis by|opinion by|author(?:\(s\))?|authors?|writer(?:s)?|instructor|lecturer|professor)\s*[:\-]?\s*",
        "",
        candidate,
        flags=re.I,
    )
    # OCR title pages sometimes join a single-letter middle initial to an
    # all-caps surname (``WARREN I.SUSMAN``). This is a spacing repair, not a
    # name guess: keep the original letters and only restore the separator.
    candidate = re.sub(r"\b([A-Z])\.([A-Z]{2,})\b", r"\1. \2", candidate)
    candidate = re.sub(r"\b(?:with a foreword by|foreword by)\b.*$", "", candidate, flags=re.I)
    candidate = re.sub(r"\([^)]*(?:@|www\.|http|doi:)[^)]*\)", "", candidate, flags=re.I)
    candidate = re.sub(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "", candidate, flags=re.I)
    candidate = candidate.strip(" ,;:-")
    if "," in candidate and not re.search(r"\b(?:Jr|Sr|III|IV|V)\b", candidate):
        parts = [part.strip(" ,;:-") for part in candidate.split(",") if part.strip(" ,;:-")]
        if len(parts) == 2:
            # Library and inter-library-loan exports commonly carry
            # ``Surname, First (First M.)``. The parenthetical is a fuller
            # given name, not a second author.
            surname, given = parts
            expanded_given = re.fullmatch(r"\s*([^()]+?)\s*\(([^()]+)\)\s*", given)
            if expanded_given:
                alternate = expanded_given.group(2).strip()
                if re.fullmatch(r"[A-Za-zÀ-ÖØ-öø-ÿ.' -]{2,50}", alternate):
                    given = alternate
                else:
                    given = expanded_given.group(1)
            candidate = f"{given} {surname}".strip()
    # Parenthetical contact/affiliation material is not part of a person's
    # name. Keep a name-only parenthetical above (the catalog case), but drop
    # common institutional addenda in ordinary visible bylines.
    candidate = re.sub(
        r"\s*\((?:[^)]*\b(?:university|department|college|school|institute|press|email|@|www\.|doi:)\b[^)]*)\)",
        "",
        candidate,
        flags=re.I,
    )
    candidate = re.sub(r"\s*\(\s*\)", "", candidate)
    candidate = re.sub(r"\s+", " ", candidate).strip(" ,;:-—–")
    return candidate


def looks_like_non_person_byline(value):
    """Recognize an explicit collective/outlet credit without inventing a person."""
    candidate = normalize_author_candidate(value).casefold()
    if not candidate:
        return False
    return any(term in candidate for term in AUTHOR_NON_PERSON_BYLINE_TERMS)


def author_phrase_is_title_fragment(candidate, title_hint=""):
    """Whether a bare candidate is a phrase taken from the document title.

    Letter case and dash glyphs vary heavily across PDF extraction, so compare
    words rather than raw strings.  This only affects weak bare-name fallback;
    explicit visible credits remain authoritative.
    """
    candidate_words = re.findall(r"[a-z0-9]+", normalize_text(candidate).casefold())
    title_words = re.findall(r"[a-z0-9]+", normalize_text(title_hint).casefold())
    if len(candidate_words) < 2 or not title_words:
        return False
    candidate_phrase = " ".join(candidate_words)
    title_phrase = " ".join(title_words)
    return candidate_phrase in title_phrase


def author_candidate_is_document_role(value):
    """Reject compact document furniture that merely has a name-like shape.

    These are grammatical roles, not a global blacklist of individual words.
    A real surname may be ``Edition`` or an organisation may contain an
    acronym; only the complete, strongly structured field/edition expressions
    below are rejected.
    """
    candidate = normalize_author_candidate(value)
    if not candidate:
        return False
    if candidate.casefold() in {"publication date", "critical paper"}:
        return True
    if looks_like_review_heading(candidate):
        return True
    return bool(
        re.fullmatch(
            r"(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|"
            r"tenth|eleventh|twelfth|\d{1,2}(?:st|nd|rd|th))\s+edition",
            candidate,
            flags=re.I,
        )
        or re.fullmatch(
            r"(?:credits?|credit\s+value|course\s+credits?|study\s+load)\s*"
            r"\([A-Z][A-Z0-9.-]{1,9}\)",
            candidate,
            flags=re.I,
        )
        or re.fullmatch(
            r"(?:cross\s+ref(?:erence)?\s+id|site\s+(?:(?:mobile|search)\s+)?navigation)",
            candidate,
            flags=re.I,
        )
    )


def author_candidate_is_font_metadata(value):
    """Recognize a font-family/style value stored in the PDF Author field.

    Publishing software sometimes writes values such as ``Dorel Extra Bold``
    into uncontrolled author metadata.  Require an actual typography style
    sequence at the end; a person whose surname happens to be Bold is not
    rejected without the accompanying style descriptor.
    """
    candidate = normalize_author_candidate(value)
    return bool(
        re.fullmatch(
            r".{2,60}\s+(?:extra|semi|demi|ultra)\s+"
            r"(?:bold|light|condensed|expanded)(?:\s+(?:italic|oblique))?",
            candidate,
            flags=re.I,
        )
        or re.fullmatch(r".{2,60}\s+(?:regular|roman)\s+(?:italic|oblique)", candidate, flags=re.I)
    )


def looks_like_person_name(value, title_hint="", *, allow_all_caps=True):
    candidate = normalize_author_candidate(value)
    if not candidate:
        return False
    if len(candidate) < 5 or len(candidate) > 80:
        return False
    lowered = candidate.casefold()
    if author_candidate_is_document_role(candidate):
        return False
    if any(term in lowered for term in AUTHOR_STOP_TERMS):
        return False
    if author_phrase_is_title_fragment(candidate, title_hint):
        return False
    if ":" in candidate:
        return False
    if re.search(r"\b(?:(?:trans|pp|vol)\.|translated\s+by\b|reproduced\s+(?:by|from)\b)", lowered):
        return False
    # Slash-separated strings such as ``Geneva/Addis Ababa`` are publication
    # datelines, not person names.  Keeping this syntactic rule narrow avoids
    # guessing a locality from ordinary title text.
    if "/" in candidate or "|" in candidate or "&" in candidate:
        return False
    if looks_like_non_person_byline(candidate):
        return False
    if re.search(r"\b(?:film|feminist|critical)\s+theory\b", lowered):
        return False
    # These are lexical markers, not arbitrary substrings: ``Fleissner``
    # contains the character sequence ``issn`` and is a perfectly valid
    # surname.  Require a standalone protocol/identifier marker instead.
    if re.search(r"(?:https?://|\bwww\.|@|\b(?:doi|issn|url)\b)", lowered):
        return False
    if re.search(r"\d", candidate):
        return False
    if candidate.count(" ") > 5:
        return False
    # ``\w`` in Unicode mode includes letters such as Ł, É, and Ø. Exclude
    # digits/underscores explicitly so author validation remains strict while
    # not silently dropping legitimate non-ASCII personal names.
    words = re.findall(r"[^\W\d_][\w'.-]*", candidate, flags=re.UNICODE)
    if len(words) < 2 or len(words) > 5:
        return False
    if any(word.casefold().rstrip(".") in AUTHOR_ORGANIZATION_TERMS for word in words):
        return False
    if any(word.casefold() in HEADING_STOPWORDS for word in words):
        return False
    # A strong, explicit byline can legitimately use all caps (for example,
    # ``BY IAN WARD``). A generic title-block fallback cannot safely make the
    # same assumption: outlet/topic banners such as ``CATHOLIC NEWS`` were
    # otherwise appended to a real author in production metadata.
    if not allow_all_caps and all(word.isupper() for word in words):
        return False
    capitalized = sum(
        1
        for word in words
        if word[0].isupper() or word.casefold() in AUTHOR_NAME_PARTICLES
    )
    if capitalized < max(2, len(words) - 1):
        return False
    return True


def looks_like_publisher_imprint_line(value):
    """Whether a single neighboring line is a plausible publisher imprint."""
    line = normalize_text(value or "").casefold()
    if not line or len(line) > 160:
        return False
    return bool(
        re.search(
            r"\b(?:press|publisher(?:s)?|university\s+press|institution\s+press|"
            r"academic\s+press|scholarly\s+press)\b",
            line,
        )
    )


def strip_known_extraction_structure_labels(text):
    """Remove parser classification prefixes, never source bracket text broadly.

    Unstructured may emit its own labels at the start of a recovered line.
    They are not source wording and can make ``[NarrativeText] More recently``
    appear to be a capitalized personal name. Restrict removal to this small,
    known set rather than stripping arbitrary bracketed source content.
    """
    labels = (
        "NarrativeText|UncategorizedText|Title|ListItem|Header|Footer|"
        "Caption|FigureCaption|Table|Address|EmailAddress|Image|Formula"
    )
    return re.sub(rf"(?mi)^\s*\[(?:{labels})\]\s*", "", str(text or ""))


def has_author_affiliation_hint(value, *, max_chars=180):
    """Recognize a compact affiliation tail without treating prose as one."""
    normalized = normalize_text(value or "")
    if not normalized or len(normalized) > max(40, int(max_chars or 180)):
        return False
    lowered = normalized.casefold()
    return any(re.search(rf"\b{re.escape(hint)}\b", lowered) for hint in AUTHOR_AFFILIATION_HINTS)


def split_author_line_candidates(line, title_hint="", *, allow_all_caps=True, require_complete=False):
    # Reject citation furniture before removing numbers or splitting names.
    # Otherwise an anthology title and its editor can become two 'authors'.
    if re.search(r"\b(?:(?:trans|pp|vol)\.|translated\s+by\b|reproduced\s+(?:by|from)\b|edited\s+by\b)|\b(?:18|19|20)\d{2}\b", line or "", re.I):
        return []
    raw = re.sub(r"[*†‡§¶∗0-9]+", " ", line or "")
    raw = re.sub(r"\s+", " ", raw).strip(" ,;:-")
    if not raw or "@" in raw:
        return []
    if has_author_affiliation_hint(raw):
        return []
    pieces = [
        piece.strip(" ,;:-")
        for piece in re.split(
            r"\s*(?:,|;|·|•|&|\band\b)\s*(?!\b(?:Jr|Sr|III|IV|V)\b)",
            raw,
            flags=re.I,
        )
        if piece.strip(" ,;:-")
    ]
    if len(pieces) < 2:
        return []
    candidates = []
    for piece in pieces:
        if looks_like_person_name(piece, title_hint=title_hint, allow_all_caps=allow_all_caps):
            candidates.append(normalize_author_candidate(piece))
        elif require_complete:
            return []
    return candidates if len(candidates) >= 2 else []


def extract_adjacent_person_names(line, title_hint="", *, allow_all_caps=True):
    digit_markers = re.findall(r"(?:[*†‡§¶∗]?\s*\d+)", line or "")
    if len(digit_markers) >= 2:
        split_parts = [
            normalize_author_candidate(part)
            for part in re.split(r"\s*(?:[*†‡§¶∗]?\s*\d+)\s*", line or "")
            if normalize_author_candidate(part)
        ]
        split_candidates = [
            part for part in split_parts
            if looks_like_person_name(part, title_hint=title_hint, allow_all_caps=allow_all_caps)
        ]
        if len(split_candidates) >= 2:
            return split_candidates

    raw = re.sub(r"[*†‡§¶∗0-9]+", " ", line or "")
    raw = re.sub(r"\s+", " ", raw).strip(" ,;:-")
    if not raw or "@" in raw:
        return []
    if has_author_affiliation_hint(raw):
        return []
    # Pulling capitalized pairs out of an arbitrary title line is exactly how
    # a subtitle such as ``Feminist Video Revolution`` became an "author".
    # Use this recovery only when the line structurally advertises a list of
    # people (footnote markers or explicit list separators). Ordinary bare
    # title-block names remain handled by the direct candidate path below.
    if not re.search(r"(?:[*†‡§¶∗]\s*\d|[;·&]|\band\b)", raw, flags=re.I):
        return []
    matches = re.findall(r"\b(?:[A-Z][A-Za-z'.-]*\s+){1,3}[A-Z][A-Za-z'.-]*\b", raw)
    candidates = []
    for match in matches:
        candidate = normalize_author_candidate(match)
        if (
            looks_like_person_name(candidate, title_hint=title_hint, allow_all_caps=allow_all_caps)
            and candidate not in candidates
        ):
            candidates.append(candidate)
    return candidates if len(candidates) >= 2 else []
