"""Read responsibility relations from text spans, without inventing page layout.

Fields and explicit publishing acts survive newline loss. Their work and role
remain essential: an editor of a series, a reviewer and a copyright owner do
not automatically author the selected work. Offsets refer to the supplied text.
"""

import re

from .names import looks_like_person_name, normalize_author_candidate
from .structured_credit import _names, _words


_FIELD = re.compile(
    r"\b(?:Chapter Author\(s\)|Book Author\(s\)|Author\(s\)|Authors?|"
    r"Review by|Edited by|Written by|Editors?|Translator|Translated by)(?:\s*:\s*|\s+)",
    re.I,
)
_NEXT = re.compile(
    r"\b(?:Publication Date|Copyright Information|Permalink|Journal|Source|"
    r"Published by|Book Title|Book Subtitle|Chapter Title|Stable URL|DOI|ISBN|"
    r"ISSN|Pub[.] Date|Product|Access Date|Publishing Company|Publisher|Online ISBN|City|Description|Identifiers|Subjects|Classification|"
    r"To cite this article|To link to this article|Follow this|"
    r"Abstract|Keywords?|Received|Revised|Accepted|Published online)\s*:?(?=\s|$)",
    re.I,
)
_ROLE = re.compile(
    r"\b(?P<role>review(?:ed)?|edited|written|foreword|preface|afterword|"
    r"translated|translation|illustrated|photograph(?:y)?|cover design|"
    r"additional reporting|reporting|interview|compiled|prepared)\s+by\s+",
    re.I,
)
_WRAPPER = re.compile(
    r"eScholarship|Recommended Citation|Citation for published version|"
    r"Stable URL|Published by:|Publication Date|Book Title:",
    re.I,
)
_BOUNDARY = re.compile(
    r"\b(?:Abstract|Summary|Introduction|Keywords?|Book Reviewed|Reviewed Work|"
    r"Staff Reporter|Research Associate|Research Fellow|Published|Copyright|"
    r"All rights reserved|First published|Third edition|Second edition|"
    r"Original Research Article|Article reuse guidelines)\b",
    re.I,
)


def _people(value):
    value = value.strip(" \n\t:;—–-")
    if re.search(
        r"\b(?:press|publishers?|publishing|group|ltd|incorporated|copyright)\b",
        value,
        re.I,
    ):
        return []
    value = re.sub(r"(?<=\w)[*†‡\d]+(?=\s|$)", "", value)
    value = re.sub(r"(?<!\s)\s+(?:editors?|authors?)\.?$", "", value, flags=re.I)
    # Spaced display dashes can separate complete people in an explicit role
    # field. An internal surname hyphen remains part of the person's name.
    displayed = re.split(r"(?<!\s)\s+[–—-]\s+", value)
    if len(displayed) > 1:
        rosters = [_names(part) for part in displayed]
        if all(rosters):
            return [person for roster in rosters for person in roster]
    names = _names(value)
    if names:
        return names
    # A labelled catalogue field can use inverted order. Preserve the roster:
    # never split each comma into a separate supposed author.
    inverted = re.split(
        r"(?<!\s)\s+and\s+|(?<!\s)\s+;\s*|;\s*", value, flags=re.I
    )
    if all(part.count(",") == 1 for part in inverted):
        names = [normalize_author_candidate(part) for part in inverted]
        if all(looks_like_person_name(name, allow_all_caps=True) for name in names):
            return names
    return []


def _credit(value):
    """An explicit following field bounds a complete roster, not a name prefix."""
    value = _NEXT.split(value, maxsplit=1)[0]
    value = _BOUNDARY.split(value, maxsplit=1)[0]
    # CIP physical-description shorthand follows the responsibility statement;
    # it is not an extension of the final contributor's surname.
    value = re.split(r"\bp[.]\s*cm[.](?=\s|$)", value, maxsplit=1, flags=re.I)[0]
    display_parts = re.split(r"(?<!\s)\s+[—–]\s+", value)
    if len(display_parts) > 1 and not all(_names(part) for part in display_parts):
        value = display_parts[0]
    # Preserve a wrapped roster within its paragraph. A blank line ends the
    # credit block, while "and Name" on the next line continues it.
    # Display spacing does not terminate a roster after an explicit separator.
    value = re.sub(r"([,;])(?:[ \t]*\n){2,}(?=[ \t]*[^\W\d_])", r"\1\n", value)
    value = re.sub(r"\n\s*\n(?=[^\S\n]*(?:and\b|&))", "\n", value, flags=re.I)
    value = re.sub(r"(?im)(\band|&)\s*\n\s*\n", r"\1\n", value)
    value = re.split(r"\n\s*\n", value.strip(), maxsplit=1)[0]
    parts = [line.strip() for line in value.splitlines() if line.strip()]
    best, joined = [], ""
    from .book import _IMPRINT

    for index, line in enumerate(parts):
        if (
            best
            and _IMPRINT.search(line)
            and re.search(
                r"\b(?:group|publishing|publishers?|ltd|incorporated)\b",
                " ".join(parts[index + 1 : index + 3]),
                re.I,
            )
        ):
            # A recognizable imprint plus adjacent corporate apparatus supplies
            # the role boundary even when OCR merges its logo into name text.
            break
        if re.fullmatch(r"and|&", line, re.I):
            joined += " " + line
            continue
        separator = (
            ", "
            if best
            and _people(line)
            and not re.match(r"^(?:and|&)\b", line, re.I)
            and not re.search(r"[,;]\s*$|\band\s*$|&\s*$", joined, re.I)
            else " "
        )
        candidate = joined + (separator if joined else "") + line
        names = _people(candidate)
        if not names and best:
            if re.search(r"[,;]\s*$|\band\s*$|&\s*$", candidate, re.I):
                # A trailing separator promises more roster text. Failure to
                # parse the unfinished line is not a publishing boundary.
                joined = candidate
                continue
            break
        joined = candidate
        if names:
            best = names
    if re.search(r"\band\s*$|&\s*$", joined, re.I):
        return []
    return _people(joined) if re.search(r"[,;]\s*$", joined) else best


def _claim(
    names,
    sample,
    match,
    role,
    family,
    priority,
    work="selected work",
    *,
    original=False,
):
    raw = str(sample.get("text") or "")
    compact = raw if original else " ".join(raw.split())
    end = min(len(compact), match.end() + 240)
    return {
        "author": ", ".join(names),
        "role": role,
        "work": work,
        "page": sample.get("page", 0),
        "evidence": compact[match.start() : end],
        "span": [match.start(), end],
        "span_coordinate_space": "original_page_text"
        if original
        else "whitespace_normalized_page_text",
        "family": family,
        "priority": priority,
        "source": "text_scoped_responsibility_span",
        "extraction": sample.get("extraction", "native_or_supplied_text"),
    }


def claims(samples, *, kind, work_scope, title_hint="", opening_pages=None):
    """Return all explicit relations; a first match never ends the source review."""
    result = []
    for sample in samples:
        text = str(sample.get("text") or "")
        compact = " ".join(text.split())
        page = int(sample.get("page") or 0)
        opening = (
            page in opening_pages if opening_pages is not None else 0 <= page <= 12
        )
        wrapper = opening and bool(
            _WRAPPER.search(compact[:2400])
            or re.search(r"(?im)^\s*(?:Authors?|Author\(s\)|Editors?)\s*:", text[:2400])
        )
        if wrapper:
            fields = list(_FIELD.finditer(compact[:2400]))
            for index, marker in enumerate(fields):
                stop = (
                    fields[index + 1].start()
                    if index + 1 < len(fields)
                    else len(compact)
                )
                raw = compact[marker.end() : stop]
                names = _credit(raw)
                if not names:
                    continue
                label = marker.group(0).casefold()
                role, work, priority = "author", "selected work", 107
                prefix = compact[max(0, marker.start() - 40) : marker.start()]
                if re.search(r"\bseries\s*$", prefix, re.I):
                    role, work, priority = "series_editor", "series", 0
                elif "translated" in label or "translator" in label:
                    role, work, priority = "translator", "translation", 0
                elif "book author" in label:
                    # Keep the parent credit; monograph inheritance needs a
                    # separate scope decision, never an editor-to-chapter leap.
                    work = "parent book" if work_scope == "chapter" else "selected work"
                    priority = 0 if work == "parent book" else 107
                elif "editor" in label or "edited" in label:
                    role = "editor"
                    if work_scope == "whole_issue":
                        work, priority = "periodical masthead", 0
                result.append(
                    _claim(
                        names,
                        sample,
                        marker,
                        role,
                        "labelled_responsibility_field",
                        priority,
                        work,
                    )
                )
        if not opening or work_scope == "whole_issue":
            continue
        if 1 <= page <= 5 or (opening_pages is not None and page in opening_pages):
            # Explicit local By lines remain interpretable even when the
            # selected filename and Info title are both neutral. The opening
            # heading and following discourse supply the work boundary.
            for byline in re.finditer(r"(?im)^\s*by\s+([^\n]+)", text):
                names = _people(byline[1])
                heading = text[: byline.start()]
                after = text[byline.end() :]
                # A list of separately credited publications is not one local
                # title/byline unit. Only a matching immediately preceding
                # title can bind an entry to the selected work.
                acts = list(
                    re.finditer(r"(?im)^\s*(?:by|edited by|written by)\s+", text)
                )
                listing = len(acts) > 1 or bool(
                    re.search(
                        r"\b(?:a series edited by|also (?:available|by)|other books)\b",
                        heading,
                        re.I,
                    )
                )
                local_heading = re.split(r"\n\s*\n", heading.rstrip())[-1]
                expected_words = _words(title_hint)
                local_words = _words(local_heading)
                overlap = min(3, len(expected_words))
                listed_title_bound = overlap >= 2 and any(
                    local_words[n : n + overlap] == expected_words[:overlap]
                    for n in range(max(0, len(local_words) - overlap + 1))
                )
                chapter_bound = work_scope != "chapter" or bool(
                    re.search(r"(?im)^\s*(?:chapter\s*)?\d{1,3}[.:]?\s+", heading)
                )
                if (
                    names
                    and (not listing or listed_title_bound)
                    and chapter_bound
                    and 1 <= len(_words(heading)) <= 45
                    and not re.search(
                        r"\b(?:praise|endorsements|contents|references|bibliography|foreword)\b",
                        heading,
                        re.I,
                    )
                    and (
                        re.search(r"(?im)^\s*(?:Abstract|Introduction)\b", after)
                        or len(_words(after)) >= 30
                        or re.search(
                            r"\b(?:university\s+press|publishers?|ISBN)\b",
                            after[:300],
                            re.I,
                        )
                    )
                ):
                    rereads = [
                        r
                        for r in sample.get("direct_byline_rereads", [])
                        if _words(r["text"]) == _words(byline[0])
                        and (r.get("confidence") or 0) >= 80
                    ]
                    visible = [
                        _words(v) for v in sample.get("visible_byline_texts", [])
                    ]
                    native_visible = _words(byline[0]) in visible
                    byline_priority = (
                        108
                        if native_visible or (rereads and not visible)
                        else 104
                        if rereads and visible
                        else 106
                    )
                    result.append(
                        _claim(
                            names,
                            sample,
                            byline,
                            "author",
                            "local_explicit_byline",
                            byline_priority,
                            original=True,
                        )
                    )
                    if native_visible:
                        result[-1]["native_visible_byline_support"] = True
                    if rereads:
                        result[-1].update(
                            reread_basis="Direct visible byline reread challenges an invisible OCR transcription",
                            reread_confidence=min(r["confidence"] for r in rereads),
                        )
        # Explicit prose responsibility is admitted in the opening apparatus,
        # before article prose. Quotes/reviews of other books remain separate.
        zone = text
        cutoff = re.search(r"\b(?:Abstract|Introduction|Keywords?)\s*:", zone, re.I)
        if cutoff:
            zone = zone[: cutoff.start()]
        for marker in _ROLE.finditer(zone):
            prefix = zone[max(0, marker.start() - 45) : marker.start()]
            if re.search(
                r"\b(?:series|volume|book reviewed|reviewed works?)\s*$", prefix, re.I
            ):
                continue
            raw = zone[marker.end() :]
            names = _credit(raw)
            if not names:
                continue
            role = marker.group("role").casefold()
            # A citation can start with the selected essay's author/title and
            # then name its containing collection's editors. Title overlap
            # alone does not bind those editors to the essay.
            parent_editor = role in {"edited", "compiled"} and bool(
                re.search(
                    r"(?:\bIn\s+[^.!?]{3,180}|\bin\s+[^.!?]{3,180},\s*)$",
                    zone[: marker.start()],
                )
            )
            if parent_editor:
                result.append(
                    _claim(
                        names,
                        sample,
                        marker,
                        "editor",
                        "explicit_publishing_act_span",
                        0,
                        "parent book",
                        original=True,
                    )
                )
                continue
            selected = role in {"written", "review", "reviewed", "edited", "compiled"}
            if role in {"review", "reviewed"} and not re.search(
                r"\breview\b", zone[: marker.start()], re.I
            ):
                selected = False
            if selected:
                # The relation needs a visible work title, not just the word
                # 'by' in a quotation. Short titles remain legitimate.
                expected = _words(title_hint)
                # A series advertisement can mention the selected title and
                # then credit several other books. The intervening plain By
                # attribution closes that title's relation just as Edited by
                # does. The next editor cannot inherit a distant title match.
                previous_acts = list(
                    re.finditer(r"\bby\s+", zone[: marker.start()], re.I)
                )
                start_at = previous_acts[-1].end() if previous_acts else 0
                prior = _words(zone[start_at : marker.start()])
                overlap = min(3, len(expected))
                title_bound = overlap >= 2 and any(
                    prior[n : n + overlap] == expected[:overlap]
                    for n in range(max(0, len(prior) - overlap + 1))
                )
                if not title_bound and not wrapper:
                    continue
            result.append(
                _claim(
                    names,
                    sample,
                    marker,
                    "editor"
                    if role in {"edited", "compiled"}
                    else "author"
                    if selected
                    else role,
                    "explicit_publishing_act_span",
                    105 if selected else 0,
                    "selected work" if selected else "related contribution",
                    original=True,
                )
            )
    return result
