"""Author evidence from explicit filename catalog structures."""
import re
from pathlib import Path
from rag_pdf_tools import normalize_text
from .constants import (
    AUTHOR_ROLE_HINTS, AUTHOR_BLOCK_STOP_HINTS, HEADING_STOPWORDS,
    AUTHOR_ORGANIZATION_TERMS,
)
from .names import normalize_author_candidate, looks_like_person_name, split_author_line_candidates


def infer_author_from_filename(path: Path, title_hint=""):
    stem = normalize_text(path.stem)
    stem = re.sub(r"\[[^\]]+\]$", "", stem).strip()
    # Explicit catalog-style prefixes are useful when a chapter excerpt omits
    # its book author from the sampled pages.  Each form below has an
    # independent delimiter/section/year cue; none treats a filename's first
    # capitalized word as an author merely because it is capitalized.
    compact_before_year = re.match(
        r"^([A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ'’-]+)([A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ'’-]+)[_-](?:18|19|20)\d{2}(?=[_-]|$)",
        stem,
    )
    if compact_before_year:
        candidate = normalize_author_candidate(" ".join(compact_before_year.groups()))
        if looks_like_person_name(candidate, allow_all_caps=False):
            return {"author": candidate, "source": "filename_compact_name_before_year", "page": 0, "evidence": path.name}
    name_before_section = re.match(
        r"^([A-ZÀ-ÖØ-Þ][^\W\d_][\w'’-]+(?:\s+[A-ZÀ-ÖØ-Þ](?:\.)?)?\s+[A-ZÀ-ÖØ-Þ][^\W\d_][\w'’-]+)\s+(?:chapter|article|essay|paper)\b",
        stem,
        flags=re.I | re.UNICODE,
    )
    if name_before_section:
        candidate = normalize_author_candidate(name_before_section.group(1))
        if (
            not re.search(r"\b(?:keyword|keywords)\b", candidate, flags=re.I)
            and not any(
                len(token) >= 2 and token.isupper()
                for token in candidate.split()
            )
            and looks_like_person_name(candidate, allow_all_caps=False)
        ):
            return {"author": candidate, "source": "filename_name_before_section_label", "page": 0, "evidence": path.name}
    surname_before_year = re.match(
        r"^([A-ZÀ-ÖØ-Þ][A-ZÀ-ÖØ-Þ'’-]{2,})[-_](?:18|19|20)\d{2}(?=[_-]|$)",
        stem,
    )
    if surname_before_year:
        return {"author": surname_before_year.group(1).title(), "source": "filename_surname_before_year", "page": 0, "evidence": path.name}
    # Course packs commonly number excerpts as ``Surname_1 Title`` or
    # ``Surname 4``. The section number is the independent catalog cue: this
    # does not generalize an arbitrary first filename word into an author.
    # Reject document-role prefixes contextually so ``Chapter 1`` and
    # ``Paper_2`` cannot become plausible-looking surnames.
    surname_before_section_number = re.match(
        r"^([A-ZÀ-ÖØ-Þ][^\W\d_](?:[^\W\d_]|['’-]){1,39})[ _-]+\d{1,3}(?=[ _.:_-]|$)",
        stem,
        flags=re.UNICODE,
    )
    if surname_before_section_number:
        candidate = normalize_text(surname_before_section_number.group(1))
        disallowed_numbered_prefixes = {
            "article", "chapter", "document", "excerpt", "file", "paper",
            "part", "poem", "poems", "scan", "section", "source", "volume",
            "vol", "week",
        }
        if (
            candidate.casefold() not in disallowed_numbered_prefixes
            and candidate.casefold() not in AUTHOR_ROLE_HINTS
            and candidate.casefold() not in AUTHOR_BLOCK_STOP_HINTS
            and candidate.casefold() not in HEADING_STOPWORDS
            and candidate.casefold() not in AUTHOR_ORGANIZATION_TERMS
        ):
            return {
                "author": candidate,
                "source": "filename_surname_before_section_number",
                "page": 0,
                "evidence": path.name,
            }
    # Course libraries and publisher downloads commonly use an explicit
    # catalog prefix: ``First Last - Title`` or ``Surname - Title``.  This is
    # stronger than guessing from a terminal title fragment because the
    # delimiter establishes the prefix's role and the remainder establishes
    # that it is followed by a title.  Keep the single-token form as surname
    # evidence only; do not generalize arbitrary first words into authors.
    leading_credit = re.match(r"^(.{2,80}?)\s+[-–—]{1,2}\s+(.{4,})$", stem)
    if leading_credit:
        prefix = normalize_author_candidate(leading_credit.group(1))
        trailing_title = normalize_text(leading_credit.group(2))
        prefix_words = re.findall(r"[^\W\d_][\w'.-]*", prefix, flags=re.UNICODE)
        prefix_is_complete_title = (
            normalize_text(prefix).casefold()
            == normalize_text(title_hint).casefold()
        )
        # ``Name - Title`` is an explicit catalog credit, but ``Book Title -
        # complete book`` uses the same punctuation for an artifact label.
        # Reject only a narrow set of generic right-hand labels; ordinary
        # chapter/article titles remain eligible.
        trailing_is_generic_artifact_label = bool(
            re.fullmatch(
                r"(?:complete|full)\s+(?:book|text|volume)|"
                r"(?:book|document|file|pdf)\s+(?:complete|copy|extract|scan)|"
                r"(?:complete|full|combined|merged|print|searchable|ocr)\s+(?:pdf|copy)",
                trailing_title,
                flags=re.I,
            )
        )
        prefix_is_catalog_title_or_slug = (
            "_" in prefix
            or bool(re.search(r"\b(?:project|studies)\b", prefix, flags=re.I))
        )
        surname_pair = re.fullmatch(
            r"([A-ZÀ-ÖØ-Þ][^\W\d_][\w'.-]*)\s+(?:and|&)\s+"
            r"([A-ZÀ-ÖØ-Þ][^\W\d_][\w'.-]*)",
            prefix,
            flags=re.UNICODE,
        )
        if surname_pair and not prefix_is_complete_title:
            return {
                "author": f"{surname_pair.group(1)}, {surname_pair.group(2)}",
                "source": "filename_leading_surnames",
                "page": 0,
                "evidence": path.name,
            }
        # A catalog prefix can contain two or more complete names, not only
        # two bare surnames: ``First Last and First Last - Title``.  The shared
        # author-line splitter already validates each person independently and
        # rejects title/prose fragments.  Reuse it here instead of weakening
        # the single-person shape rule or adding a document-specific exception.
        leading_names = split_author_line_candidates(
            prefix,
            # The explicit ``credit - title`` delimiter establishes that this
            # is the credit field. PDF title metadata often repeats that field
            # verbatim (``Names - Site``); passing it back as a title hint
            # would incorrectly reject the very author evidence being parsed.
            title_hint="",
            allow_all_caps=False,
        )
        if len(leading_names) >= 2 and not prefix_is_complete_title:
            return {
                "author": ", ".join(leading_names[:12]),
                "source": "filename_leading_names",
                "page": 0,
                "evidence": path.name,
            }
        if (
            not prefix_is_complete_title
            and not trailing_is_generic_artifact_label
            and not prefix_is_catalog_title_or_slug
            and looks_like_person_name(
                prefix,
                # The delimiter establishes that this is the credit field.
                # Passing the filename-derived title back here made every
                # valid single-person credit look like a title fragment.
                title_hint="",
                allow_all_caps=False,
            )
        ):
            return {
                "author": prefix,
                "source": "filename_leading_name",
                "page": 0,
                "evidence": path.name,
            }
        if (
            not prefix_is_complete_title
            and len(prefix_words) == 1
            # An underscore inside the prefix marks a filename/title slug,
            # not a single surname (``Theory_Theatre - Ch.4``).
            and "_" not in prefix
            and 2 <= len(prefix_words[0]) <= 40
            and prefix_words[0][0].isupper()
        ):
            return {
                "author": prefix_words[0],
                "source": "filename_leading_surname",
                "page": 0,
                "evidence": path.name,
            }
    # Numbered excerpts often omit spaces around the title delimiter, for
    # example ``De Grazia 02-Irresistible Empire``.  A name-shaped prefix
    # followed by an explicit section number is still a catalog convention;
    # the number is never part of the author.
    numbered_credit = re.match(
        r"^([A-ZÀ-ÖØ-Þ][^\W\d_][\w'.-]*(?:\s+(?:(?:de|del|della|di|du|la|le|van|von|der|den|ter)|[A-ZÀ-ÖØ-Þ][^\W\d_][\w'.-]*)){1,3})\s+\d{1,3}\s*[-_:]",
        stem,
        flags=re.UNICODE,
    )
    if numbered_credit:
        prefix = normalize_author_candidate(numbered_credit.group(1))
        if looks_like_person_name(prefix, allow_all_caps=False):
            return {
                "author": prefix,
                "source": "filename_leading_name_before_section",
                "page": 0,
                "evidence": path.name,
            }
    # Browser downloads often preserve an explicit byline as a slug, e.g.
    # ``article-title-by-First-Last.pdf``.  This is weaker than text on the
    # page, but much stronger than treating title words as a person.
    slug_byline = re.search(
        r"(?:^|[-_\s])(?:written[-_\s]+)?by[-_\s]+([A-Za-z][A-Za-z'.-]*(?:[-_\s]+[A-Za-z][A-Za-z'.-]*){1,4})$",
        stem,
        flags=re.I,
    )
    if slug_byline:
        candidate = re.sub(r"[-_]+", " ", slug_byline.group(1))
        candidate = normalize_author_candidate(candidate)
        # The filename itself necessarily contains this candidate, so title
        # fragment protection would reject every explicit ``-by-Name`` slug.
        if looks_like_person_name(candidate):
            return {"author": candidate, "source": "filename_explicit_byline", "page": 0, "evidence": path.name}
    # Some course/archive filenames preserve a terminal ``-first-last``
    # credit after an underscore-delimited title slug. This is deliberately
    # narrower than treating the last two words of every dashed title as a
    # person (``The-Yellow-Wall-Paper`` must never become ``Wall Paper``).
    terminal_slug_name = re.search(
        r".+_.+?-([A-Za-z][A-Za-z'.-]*)-([A-Za-z][A-Za-z'.-]*)$",
        stem,
    )
    if terminal_slug_name:
        candidate = " ".join(part.capitalize() for part in terminal_slug_name.groups())
        if looks_like_person_name(candidate):
            return {
                "author": candidate,
                "source": "filename_terminal_name_slug",
                "page": 0,
                "evidence": path.name,
            }
    parts = [part.strip(" -_,") for part in re.split(r"\s+[-–—]{1,2}\s+|\s{2,}", stem) if part.strip(" -_,")]
    tail_candidates = list(reversed(parts[1:])) if len(parts) >= 2 else []
    for candidate in tail_candidates:
        cleaned = re.sub(r"\b(?:paperback|hardcover|preview|ocr|edition|\d{4})\b", " ", candidate, flags=re.I)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,;:-")
        if looks_like_person_name(cleaned, title_hint=title_hint):
            return {"author": normalize_author_candidate(cleaned), "source": "filename_author_fallback", "page": 0, "evidence": path.name}
    return {"author": "", "source": "not_found", "page": 0, "evidence": ""}


def structured_filename_surname_evidence(path: Path):
    """Return surname tokens only from explicit, non-free-form filename fields."""
    stem = normalize_text(path.stem)
    evidence = []
    match = re.match(r"^([A-Za-zÀ-ÖØ-öø-ÿ'’-]{3,})-(?:18|19|20)\d{2}-", stem)
    if match:
        evidence.append(match.group(1).casefold())
    match = re.match(r"^([A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’-]{2,})-(?=[A-ZÀ-ÖØ-Þ])", stem)
    if match:
        evidence.append(match.group(1).casefold())
    fields = [field.strip() for field in re.split(r"\s+--\s+", stem)]
    if len(fields) >= 3:
        author_field = fields[1]
        for segment in re.split(r"\s*(?:;|,\s+(?=[A-ZÀ-ÖØ-Þ]))\s*", author_field):
            segment = re.sub(r"\s*\((?:editor|ed\.?|author)\)\s*", "", segment, flags=re.I)
            tokens = re.findall(r"[^\W\d_]+", segment, flags=re.UNICODE)
            if len(tokens) >= 2:
                evidence.append(tokens[-1].casefold())
    return set(evidence)
