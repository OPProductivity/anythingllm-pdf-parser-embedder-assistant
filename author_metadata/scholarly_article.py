"""Scholarly title/byline/affiliation/abstract grammar.

Names in subject, interview, contributor, or body positions are not credits.
This strategy does not infer journal authors from PDF properties or filenames.
"""

import re
import unicodedata
import fitz

from .names import (normalize_author_candidate, looks_like_person_name,
                    author_phrase_is_title_fragment,
                    split_author_line_candidates, has_author_affiliation_hint)
from .title_evidence import matches, not_found, words

_ROLE = re.compile(r"\b(?:interviewees?|interview\s+with|participants?|contributors?|"
                   r"speakers?|series\s+editors?|volume\s+editors?|editorial\s+board|"
                   r"translators?|translated\s+by|acknowledgments?|acknowledgements?|references|bibliography)\b", re.I)
_BOUNDARY = re.compile(r"^(?:abstract|keywords?|introduction)\b", re.I)
_LOCATION = re.compile(r"^[^,\d]{2,40},\s*[^,\d]{2,40}$")
_DOI = re.compile(r"^(?:doi\s*:\s*|https?://(?:dx\.)?doi\.org/)10\.\S+$", re.I)
_PUBLICATION = re.compile(r"^(?:received:|revised:|accepted:|published online:|\u00a9\s|copyright\s)", re.I)
_DATE = re.compile(r"^\d{1,2}\s+[A-Za-z]+\s+\d{4}$")


def has_editorial_opening(samples):
    first = next((sample for sample in samples or ()
                  if int(sample.get("page") or 0) == 1), None)
    if first is None:
        return False
    lines = [" ".join(line.split()) for line in str(first.get("text") or "").splitlines()
             if line.strip()]
    return any(re.fullmatch(r"Editorial", line, re.I) for line in lines[:8])


def _names(line, title=""):
    if _ROLE.search(line) or has_author_affiliation_hint(line):
        return []
    suffix = re.fullmatch(r"(.+?),\s*(Jr\.?|Sr\.?|III|IV)", line, re.I)
    if suffix:
        candidate = normalize_author_candidate(line)
        return [candidate] if looks_like_person_name(candidate, title_hint=title) else []
    # A bare comma can separate two complete names on a publisher cover.
    # Require two validated names so a location such as Aalborg, Denmark
    # remains outside the author grammar.
    if "," in line and not re.search(r"\band\b|&", line, re.I):
        names = split_author_line_candidates(line, title_hint=title, require_complete=True)
        return names if len(names) >= 2 else []
    line = re.sub(r"(?<=[^\W\d_])[\d*\u2020\u2021]+(?=\s*(?:,|&|\band\b|$))", "", line)
    names = split_author_line_candidates(line, title_hint=title, require_complete=True)
    if names:
        return names
    candidate = normalize_author_candidate(line)
    return [candidate] if looks_like_person_name(candidate, title_hint=title) else []


def _matches_title(visible, hint, names):
    if matches(visible, hint, names, allow_long_prefix=True):
        return True
    parts = re.split(r"\s+(?:--|[-\u2013\u2014])\s+", hint)
    if len(parts) > 1 and sorted(words(parts[0])) == sorted(words(" ".join(names))):
        return matches(visible, " ".join(parts[1:]), names, allow_long_prefix=True)
    if len(parts) > 1 and any(
        words(parts[0]) == words(name)[-len(words(parts[0])):]
        for name in names if words(parts[0])
    ):
        return matches(visible, " ".join(parts[1:]), names, allow_long_prefix=True)
    return False


def _matches_truncated_filename_title(visible, context):
    """Allow a clipped filename to corroborate a longer visible article title."""
    if context.path is None or words(context.path.stem) != words(context.title_hint):
        return False
    expected, observed = words(context.title_hint), words(visible)
    return bool(
        len(expected) >= 5
        and len(expected[-1]) >= 5
        and len(observed) >= len(expected) + 2
        and observed[:len(expected) - 1] == expected[:-1]
        and observed[len(expected) - 1].startswith(expected[-1])
        and observed[len(expected) - 1] != expected[-1]
    )


def _result(names, sample, lines, source):
    return {"author": ", ".join(names), "source": source,
            "page": int(sample["page"]), "evidence": " / ".join(lines)}


def _corroborate_affiliation_suffix(names, lines):
    """Remove a merged lowercase affiliation mark when a credit confirms it."""
    if len(names) != 1:
        return names
    candidate = names[0]
    marker = candidate[-1:]
    if not marker.islower():
        return names
    for index, line in enumerate(lines[:100]):
        if not re.fullmatch(r"Corresponding Author\s*:?", line, re.I):
            continue
        if index + 1 >= len(lines):
            continue
        corroborated = _names(lines[index + 1].split(",", 1)[0])
        if not any(candidate[:-1].casefold() == name.casefold() for name in corroborated):
            continue
        if any(re.match(rf"^{re.escape(marker)}[A-Z].*"
                        r"(?:University|College|Department|Institute|School)",
                        value) for value in lines[:100]):
            return [name for name in corroborated if candidate[:-1].casefold() == name.casefold()]
    return names


def _truncated_cover_title_matches(visible, hint, citation_title):
    """Accept a PDF Title cut inside its final word only with the publisher citation."""
    observed, expected = words(visible), words(hint)
    cited = words(citation_title)
    return bool(
        len(expected) >= 10
        and len(observed) == len(expected)
        and observed[:-1] == expected[:-1]
        and 4 <= len(expected[-1]) < len(observed[-1]) <= len(expected[-1]) + 5
        and observed[-1].startswith(expected[-1])
        and cited[:len(observed)] == observed
    )


def _cover_credit(lines, context, sample):
    for index, line in enumerate(lines[:24]):
        labelled = re.fullmatch(r"Author\(s\):\s*(.+)", line, re.I)
        if labelled and index and any(value.startswith("Source:") for value in lines[index + 1:index + 4]):
            names = _names(labelled.group(1))
            archive = any(re.match(r"Stable URL:\s*https?://(?:www\.)?jstor\.org/", value, re.I)
                          for value in lines[index + 1:index + 6])
            title_ok = any(
                (len(words(visible)) >= 2 and words(visible) == words(context.title_hint))
                or _matches_title(visible, context.title_hint, names)
                for visible in (" ".join(lines[index - width:index])
                                for width in range(1, min(index, 5) + 1)))
            if names and archive and title_ok and not any(_ROLE.search(value) for value in lines[:index]):
                return _result(names, sample, lines[index:index + 3], "text_author_label")
        names = _names(line)
        if not names or any(_ROLE.search(value) for value in lines[:index]):
            continue
        title_candidates = [" ".join(lines[index - width:index])
                            for width in range(1, min(index, 5) + 1)]
        title_ok = any(_matches_title(value, context.title_hint, names)
                       for value in title_candidates)
        following = lines[index + 1:index + 5]
        if following and re.match(r"^To cite this article:\s*", following[0], re.I):
            citation_names = re.split(r"\s*\(\d{4}\)", re.sub(r"^To cite this article:\s*", "", following[0], flags=re.I), maxsplit=1)[0]
            citation_block = " ".join(following)
            citation_title = re.sub(r"^To cite this article:\s*.+?\(\d{4}\)\s*:?\s*", "", citation_block, flags=re.I)
            if (_names(citation_names) == names and (title_ok or any(
                    _truncated_cover_title_matches(value, context.title_hint, citation_title)
                    for value in title_candidates))):
                return _result(names, sample, [line, following[0]], "text_bibliographic_byline")
        if not title_ok:
            continue
        if (any(re.search(r"\(Article\)\s*$", value) for value in following)
                and any(re.match(r"^Published by\s+", value) for value in following)
                and any(re.match(r"https?://muse\.jhu\.edu/article/", value) for value in lines[index + 1:index + 8])):
            return _result(names, sample, [line] + following, "text_strict_credit_block")
    return not_found()


def _translated_credit(lines, context, sample):
    if not any(re.search(r"\bVol\.?\s*\d+.*\bIssue\b", value, re.I) for value in lines[:6]):
        return not_found()
    for index, line in enumerate(lines[:20]):
        if not re.fullmatch(r"Translated by\s*:?", line, re.I) or index < 2:
            continue
        if any(_ROLE.search(value) for value in lines[:index]):
            continue
        names = _names(lines[index - 1])
        if not names:
            continue
        for width in range(1, min(index - 1, 5) + 1):
            visible = " ".join(lines[index - 1 - width:index - 1])
            visible = re.sub(r"(?<=[^\W\d_])\d{1,2}$", "", visible)
            if _matches_title(visible, context.title_hint, names):
                return _result(names, sample, [lines[index - 1], line], "text_title_byline_translation")
    return not_found()


def _abstract_adjacent_credit(lines, context, sample):
    """Read a first-page byline next to a verified title and Abstract heading.

    Journal PDFs can emit the title block after a body column, or print a
    credential inside a multi-person byline. The title and abstract are both
    required; no names are harvested from the article body or correspondence.
    """
    if int(sample["page"]) != 1:
        return not_found()
    expected = words(context.title_hint)
    if len(expected) < 3:
        return not_found()

    def title_matches(value):
        observed = words(value)
        if len(observed) < 3:
            return False
        if observed == expected:
            return True
        minimum = min(4, len(expected))
        return any(
            observed[:minimum] == expected[index:index + minimum]
            for index in range(len(expected) - minimum + 1)
        )

    def credit_names(value):
        if (author_phrase_is_title_fragment(value, context.title_hint)
                or _LOCATION.fullmatch(value)
                or "(" in value or ")" in value):
            return []
        raw = re.sub(r"(?<=[^\W\d_])\d{1,2}$", "", value)
        raw = re.sub(r",?\s*(?:Ph\.?D\.?|M\.?D\.?)\.?", "", raw, flags=re.I)
        raw = re.sub(r",\s*(?=and\b)", " ", raw, flags=re.I)
        names = split_author_line_candidates(raw, require_complete=True)
        if not names:
            candidate = normalize_author_candidate(raw)
            if looks_like_person_name(candidate, title_hint="", allow_all_caps=True):
                names = [candidate]
        names = [name.title() if name.isupper() else name for name in names]
        return _corroborate_affiliation_suffix(names, lines)

    for abstract_index, line in enumerate(lines[:64]):
        if not re.match(r"^abstract\b", line, flags=re.I):
            continue
        if abstract_index < 2:
            continue
        # Standard title -> byline -> Abstract, including titles wrapped over
        # several lines. The last line before Abstract must be a complete name.
        names = credit_names(lines[abstract_index - 1])
        if names:
            for start in range(max(0, abstract_index - 9), abstract_index - 1):
                if (not any(_ROLE.search(value) for value in lines[start:abstract_index])
                        and title_matches(" ".join(lines[start:abstract_index - 1]))):
                    return _result(names, sample, lines[abstract_index - 1:abstract_index + 1],
                                   "text_strict_credit_block")
        # Some journal covers print the author above the title instead. Keep
        # the complete selected title directly between author and Abstract.
        for name_index in range(max(0, abstract_index - 7), abstract_index - 1):
            names = credit_names(lines[name_index])
            if len(names) != 1:
                continue
            if (not any(_ROLE.search(value) for value in lines[name_index:abstract_index])
                    and title_matches(" ".join(lines[name_index + 1:abstract_index]))):
                return _result(names, sample, lines[name_index:abstract_index + 1],
                               "text_strict_credit_block")
    return not_found()


def _medical_multicolumn_credit(lines, sample):
    """Recover a credentialled byline emitted after the first body column."""
    if int(sample["page"]) != 1:
        return not_found()
    abstract = next((i for i, line in enumerate(lines[:12])
                     if re.match(r"^abstract\b", line, re.I)), -1)
    contact = next((i for i, line in enumerate(lines[:100])
                    if re.fullmatch(r"Corresponding Author\s*:?", line, re.I)), -1)
    if abstract < 2 or len(words(" ".join(lines[:abstract]))) < 6 or contact < 0:
        return not_found()
    credential = re.compile(
        r"(.+?),\s*((?:(?:MD|DO|PhD|MPH|DNP|RN|MS|MAS|FAAFP)[,\s]*)+)(?:\d{1,2})?",
        re.I,
    )
    for start in range(abstract + 3, min(contact, len(lines))):
        names = []
        cursor = start
        while cursor < min(contact, len(lines)):
            match = credential.fullmatch(lines[cursor])
            parsed = _names(match.group(1)) if match else []
            if len(parsed) != 1:
                break
            names.append(parsed[0])
            cursor += 1
        if len(names) < 3 or cursor >= len(lines):
            continue
        if not re.match(r"^\d{1,2}(?:Department|University|School|College|Institute)\b",
                        lines[cursor], re.I):
            continue
        contact_names = _names(lines[contact + 1]) if contact + 1 < len(lines) else []
        if contact_names != names[:1]:
            continue
        return _result(names, sample, lines[start:cursor], "text_medical_multicolumn_byline")
    return not_found()


def _repository_cover_credit(context):
    """Cross-check a repository cover credit against the work's own byline."""
    pages = {int(sample["page"]): sample for sample in context.opening_pages}
    if 1 not in pages or 2 not in pages:
        return not_found()
    cover = [" ".join(line.split()) for line in pages[1]["text"].splitlines() if line.strip()]
    content = [" ".join(line.split()) for line in pages[2]["text"].splitlines() if line.strip()]
    if not any("researchgate.net/publication/" in line.casefold() for line in cover[:5]):
        return not_found()
    for index, line in enumerate(cover[:25]):
        if not re.fullmatch(r"1 author\s*:", line, re.I) or index + 1 >= len(cover):
            continue
        cover_names = _names(cover[index + 1])
        if len(cover_names) != 1:
            continue
        for credit_index, visible in enumerate(content[1:12], start=1):
            if _names(visible) != cover_names:
                continue
            for width in range(1, min(5, credit_index) + 1):
                title = " ".join(content[credit_index - width:credit_index])
                def key(value):
                    return tuple(word for word in words(value)
                                 if word not in {"and", "the", "of"})
                if (key(title) == key(context.title_hint)
                        and any(key(value) == key(title) for value in cover[:5])):
                    return _result(cover_names, pages[2],
                                   [title, visible, cover[index + 1]],
                                   "text_repository_cover_correlated_byline")
    return not_found()


def _openedition_cover_credit(lines, context, sample):
    """Use a visible OpenEdition credit repeated in its electronic citation."""
    if not any("journals.openedition.org/" in line for line in lines[:35]):
        return not_found()
    reference = next((i for i, line in enumerate(lines[:45])
                      if re.fullmatch(r"Electronic reference", line, re.I)), -1)
    if reference < 0:
        return not_found()
    for index, line in enumerate(lines[:reference]):
        names = _names(line)
        if len(names) != 1 or index + 1 >= len(lines):
            continue
        if not re.fullmatch(r"Electronic version", lines[index + 1], re.I):
            continue
        if not any(_matches_title(" ".join(lines[index - width:index]),
                                  context.title_hint, names)
                   for width in range(1, min(5, index) + 1)):
            continue
        if any(lines[cite].casefold().startswith(names[0].casefold() + ",")
               for cite in range(reference + 1, min(reference + 4, len(lines)))):
            return _result(names, sample, [line, lines[reference]],
                           "text_bibliographic_byline")
    return not_found()


def _editorial_end_signature(context):
    """Use an editorial's own signed closing block, not journal masthead."""
    if not has_editorial_opening(context.opening_pages):
        return not_found()
    first = next(sample for sample in context.opening_pages
                 if int(sample["page"]) == 1)
    opening = [" ".join(line.split()) for line in first["text"].splitlines() if line.strip()]
    if any(re.match(r"^Abstract\b", line, re.I) for line in opening[:25]):
        return not_found()
    for sample in sorted(context.samples, key=lambda row: int(row.get("page") or 0), reverse=True):
        if int(sample.get("page") or 0) <= 4:
            continue
        lines = [" ".join(line.split()) for line in sample["text"].splitlines() if line.strip()]
        for index, line in enumerate(lines[-45:]):
            actual = len(lines) - min(45, len(lines)) + index
            if not re.fullmatch(r"(?:Founding|Managing|Chief)?\s*Editor(?:-in-Chief)?", line, re.I):
                continue
            if actual < 1 or actual + 1 >= len(lines):
                continue
            names = _names(lines[actual - 1])
            following = " ".join(lines[actual + 1:actual + 4])
            if len(names) == 1 and has_author_affiliation_hint(following):
                return _result(names, sample, lines[actual - 1:actual + 2],
                               "text_editorial_end_signature")
    return not_found()


def _journal_opening_credit(context):
    """Use a title-adjacent credit when a journal cue has already been verified.

    Older journal layouts often lack an Abstract heading. Their opening title,
    author and affiliation stack is still a stronger credit than PDF properties.
    """
    if "journal_publication_record" not in context.profile.cues:
        return not_found()
    for sample in context.opening_pages:
        if int(sample.get("page") or 0) != 1:
            continue
        raw = str(sample.get("text") or "")
        if re.search(r"(?im)^\s*To cite this article:\s*", raw):
            continue
        if (re.search(r"(?im)^\s*Source:\s*", raw)
                and not re.search(r"\b(?:Stable URL|Vol\.|Volume|ISSN|pp\.)\b", raw, re.I)):
            continue
        lines = [" ".join(unicodedata.normalize("NFKC", line).split())
                 for line in str(sample.get("text") or "").splitlines() if line.strip()]
        if len(lines) >= 3 and all(re.fullmatch(r"[A-ZÀ-ÖØ-Þ]{3,30}", value)
                                   for value in lines[:2]):
            stacked = " ".join(lines[:2])
            title_words = words(lines[2])
            hint_words = words(context.title_hint)
            if (looks_like_person_name(stacked, allow_all_caps=True)
                    and len(title_words) >= 2
                    and any(hint_words[start:start + len(title_words)] == title_words
                            for start in range(len(hint_words) - len(title_words) + 1))):
                return _result([stacked], sample, lines[:3], "text_journal_title_byline")
        def title_matches(value):
            observed = words(re.sub(r"(?<=[^\W\d_])\d{1,2}$", "", value))
            expected = words(context.title_hint)
            segments = [words(part) for part in re.split(r"\s+(?:--|[-\u2013\u2014])\s+", context.title_hint)]
            return len(observed) >= 4 and (observed == expected or any(
                observed == segment for segment in segments if len(segment) >= 4))
        for end in range(1, min(18, len(lines) - 1)):
            if not any(title_matches(" ".join(lines[start:end]))
                       for start in range(max(0, end - 7), end)):
                continue
            names = []
            for line in lines[end:min(end + 12, len(lines))]:
                if _BOUNDARY.match(line) or _ROLE.search(line):
                    break
                inline = re.fullmatch(r"([^,]{3,80}),\s*(.+)", line)
                if inline and has_author_affiliation_hint(inline.group(2)):
                    found = _names(inline.group(1))
                elif has_author_affiliation_hint(line):
                    continue
                else:
                    found = _names(line)
                if not found:
                    break
                names.extend(name for name in found if name not in names)
            names = [re.sub(r"[\ue000-\uf8ff]", "", name).strip() for name in names]
            if names and all(looks_like_person_name(name) for name in names):
                return _result(names, sample, lines[end:end + len(names)], "text_journal_title_byline")
    return not_found()


def _spaced_abstract_credit(context):
    """Read an Elsevier analysis byline with lettered affiliation suffixes."""
    for sample in context.opening_pages:
        if int(sample.get("page") or 0) != 1:
            continue
        lines = [" ".join(line.split()) for line in str(sample.get("text") or "").splitlines()
                 if line.strip()]
        if (len(lines) < 8 or lines[0].casefold() != "analysis"
                or words(lines[1]) != words(context.title_hint)
                or not any(re.fullmatch(r"a\s+b\s+s\s+t\s+r\s+a\s+c\s+t", line, re.I)
                           for line in lines[:16])):
            continue
        cleaned = re.sub(r"\s+[a-d](?:,[a-d])+(?:,[⁎*])?", "", lines[2], flags=re.I)
        names = split_author_line_candidates(cleaned, require_complete=True)
        if len(names) >= 2 and all(looks_like_person_name(name) for name in names):
            return _result(names, sample, lines[1:3], "text_strict_credit_block")
    return not_found()


def infer(context):
    if context.profile.kind != "scholarly_article":
        return not_found()
    # A signed current editorial has its own responsibility statement. It
    # outranks name-shaped journal furniture encountered earlier in the prose.
    editorial = _editorial_end_signature(context)
    if editorial["author"]:
        return editorial
    # Magazine-style scholarly exports sometimes print a lowercase title and
    # lowercase `by` credit. The exact title phrase must occur in the selected
    # filename and the complete credit must be its immediate next line.
    first = next((sample for sample in context.opening_pages
                  if int(sample.get('page') or 0) == 1), None)
    if first is not None:
        leading = [" ".join(line.split()) for line in first['text'].splitlines() if line.strip()][:8]
        hint_words = words(context.title_hint)
        for index in range(min(5, len(leading) - 1)):
            title_words = words(leading[index])
            credit = re.fullmatch(r'by\s+(.+)', leading[index + 1], re.I)
            if not credit or len(title_words) < 5 or not any(
                hint_words[start:start + len(title_words)] == title_words
                for start in range(len(hint_words) - len(title_words) + 1)
            ):
                continue
            raw_names = re.split(r'\s+(?:and|&)\s+|\s*,\s*', credit.group(1), flags=re.I)
            names = [normalize_author_candidate(name.title()) for name in raw_names]
            if names and all(looks_like_person_name(name, title_hint=leading[index]) for name in names):
                return _result(names, first, leading[index:index + 2], 'text_byline')
    repository = _repository_cover_credit(context)
    if repository["author"]:
        return repository
    for sample in context.opening_pages:
        if int(sample["page"]) > 3:
            continue
        lines = [" ".join(unicodedata.normalize("NFKC", line).split()) for line in sample["text"].splitlines() if line.strip()][:100]
        cover = _cover_credit(lines, context, sample)
        if cover["author"]:
            return cover
        openedition = _openedition_cover_credit(lines, context, sample)
        if openedition["author"]:
            return openedition
        translated = _translated_credit(lines, context, sample)
        if translated["author"]:
            return translated
        if any(_BOUNDARY.match(line) for line in lines[:48]):
            for line in lines[:16]:
                explicit = re.fullmatch(r"(?:by|written\s+by|authors?\s*:)\s*(.+)", line, re.I)
                if explicit:
                    names = _names(explicit.group(1))
                    if names:
                        return _result(names, sample, [line], "text_byline")
        for start in range(min(80, len(lines))):
            if any(_ROLE.search(line) for line in lines[:start + 1]):
                continue
            if (
                start + 1 < len(lines)
                and _names(lines[start])
                and _names(lines[start + 1])
                and any(
                    _matches_truncated_filename_title(
                        " ".join(lines[start - width:start + 1]), context
                    )
                    for width in range(1, min(3, start) + 1)
                )
            ):
                # A wrapped title can end in two capitalized words that look
                # like a person. Its actual byline starts on the next line.
                continue
            names, credit_lines = [], []
            cursor = start
            affiliation = False
            while cursor < min(len(lines), start + 12):
                line = lines[cursor]
                if _BOUNDARY.match(line) or _ROLE.search(line):
                    break
                inline = re.fullmatch(r"([^,]{3,80}),\s*(.+)", line)
                parenthetical = re.fullmatch(r"(.{3,80}?)\s*\(([^)]{3,100})\)", line)
                if parenthetical and has_author_affiliation_hint(parenthetical.group(2)):
                    found = _names(parenthetical.group(1))
                    affiliation = True
                elif (inline and has_author_affiliation_hint(inline.group(2))
                      and _names(inline.group(1))):
                    found = _names(inline.group(1))
                    affiliation = True
                elif has_author_affiliation_hint(line) or re.search(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", line):
                    if not names:
                        break
                    affiliation = True
                    credit_lines.append(line)
                    cursor += 1
                    continue
                elif names and (_PUBLICATION.match(line) or _DATE.fullmatch(line)):
                    credit_lines.append(line)
                    cursor += 1
                    continue
                elif _LOCATION.fullmatch(line) and not _names(line):
                    # One location after an established name may precede the
                    # abstract. It is never itself parsed as a person.
                    if not names or cursor + 1 >= len(lines) or not _BOUNDARY.match(lines[cursor + 1]):
                        break
                    credit_lines.append(line)
                    cursor += 1
                    continue
                elif names and re.match(r"^(?:and|&)\s+\S", line, re.I):
                    found = _names(re.sub(r"^(?:and|&)\s+", "", line, count=1, flags=re.I))
                else:
                    found = _names(line)
                if not found:
                    break
                names.extend(name for name in found if name not in names)
                credit_lines.append(line)
                cursor += 1
            if not names or cursor >= len(lines):
                continue
            boundary = bool(_BOUNDARY.match(lines[cursor]) or (
                affiliation and re.search(r"\(\d{4}\).+\bdoi\s*:", lines[cursor], re.I)
            ))
            unlabelled = affiliation and len(" ".join(lines[cursor:cursor + 2]).split()) >= 18
            if not (boundary or unlabelled):
                continue
            title_end = start
            if title_end and _DOI.fullmatch(lines[title_end - 1]):
                title_end -= 1
            for width in range(1, min(12, title_end) + 1):
                visible_title = " ".join(lines[title_end - width:title_end])
                title_matches = (
                    _matches_title(visible_title, context.title_hint, names)
                    or _matches_truncated_filename_title(visible_title, context)
                )
                machine_title = bool(re.fullmatch(r"[A-Za-z]{2,}[_-]?\d{3,}.*", context.title_hint))
                if not title_matches and not (machine_title and width >= 2 and affiliation):
                    continue
                if not all(looks_like_person_name(name, title_hint=visible_title) for name in names):
                    continue
                names = _corroborate_affiliation_suffix(names, lines)
                return {"author": ", ".join(names), "source": "text_affiliated_byline" if affiliation else "text_strict_credit_block",
                        "page": int(sample["page"]), "evidence": " / ".join(credit_lines + [lines[cursor]])}
    for sample in context.opening_pages:
        if int(sample["page"]) != 1:
            continue
        lines = [" ".join(unicodedata.normalize("NFKC", line).split())
                 for line in sample["text"].splitlines() if line.strip()][:100]
        adjacent = _abstract_adjacent_credit(lines, context, sample)
        if adjacent["author"]:
            return adjacent
        medical = _medical_multicolumn_credit(lines, sample)
        if medical["author"]:
            return medical
    opening_credit = _journal_opening_credit(context)
    if opening_credit["author"]:
        return opening_credit
    spaced = _spaced_abstract_credit(context)
    if spaced["author"]:
        return spaced
    # PyMuPDF drawing order can put a two-column article's visible title and
    # byline after its body text. Try a positioned reading order only when
    # the existing scholarly rules abstain, and keep the same adjacent-title
    # requirement used for the native first-line credit above.
    if context.path is not None and first is not None:
        try:
            with fitz.open(context.path) as doc:
                sorted_lines = [" ".join(line.split()) for line in doc[0].get_text(sort=True).splitlines()
                                if line.strip()][:20]
                positioned_blocks = doc[0].get_text('blocks')
                page_height = float(doc[0].rect.height)
        except Exception:
            sorted_lines = []
            positioned_blocks = []
            page_height = 0
        hint_words = words(context.title_hint)
        for index in range(min(12, len(sorted_lines) - 1)):
            title_words = words(sorted_lines[index])
            credit = re.fullmatch(r'by\s+(.+)', sorted_lines[index + 1], re.I)
            if not credit or len(title_words) < 5 or not any(
                hint_words[start:start + len(title_words)] == title_words
                for start in range(len(hint_words) - len(title_words) + 1)
            ):
                continue
            names = [normalize_author_candidate(name.title()) for name in
                     re.split(r'\s+(?:and|&)\s+|\s*,\s*', credit.group(1), flags=re.I)]
            if names and all(looks_like_person_name(name, title_hint=sorted_lines[index]) for name in names):
                return _result(names, first, sorted_lines[index:index + 2], 'text_byline')
        # An excerpt may continue in one column while a new work begins in
        # another. Require a complete local title (or the work-title segment
        # after a separator), an aligned byline, and body text in that column.
        # PDF drawing order is not necessarily column order.
        title_parts = [words(part) for part in re.split(
            r'\s+(?:--|[-\u2013\u2014])\s+|:\s+', context.title_hint)]
        title_parts = [part for part in title_parts if part]
        target_title = title_parts[-1] if title_parts else []
        blocks = [(bounds, ' '.join(str(bounds[4]).split())) for bounds in positioned_blocks
                  if len(bounds) >= 5 and str(bounds[4]).strip()]
        for box, heading in blocks:
            heading_words = words(heading)
            title_match = heading_words == target_title or (
                len(target_title) == 1 and len(target_title[0]) >= 5
                and heading_words[:1] == target_title and len(heading_words) <= 2
                and '/' in heading
            )
            if not (page_height and box[1] < page_height * .28
                    and heading_words and title_match):
                continue
            aligned = [(other, line) for other, line in blocks
                       if abs(float(other[0]) - float(box[0])) <= 18
                       and float(other[1]) > float(box[3])]
            if not aligned:
                continue
            # Sort by vertical position, not by text or by PDF drawing order.
            aligned.sort(key=lambda item: float(item[0][1]))
            next_box, line = aligned[0]
            if not 0 < float(next_box[1]) - float(box[3]) < 30:
                continue
            candidate = normalize_author_candidate(line)
            if not looks_like_person_name(candidate, title_hint=heading):
                continue
            body_nearby = any(
                15 < float(body_box[1]) - float(next_box[3]) < 110
                and abs(float(body_box[0]) - float(box[0])) <= 18
                and len(words(body)) >= 8
                for body_box, body in aligned[1:]
            )
            if body_nearby:
                return _result([candidate], first, [heading, line], 'text_strict_credit_block')
    return not_found()
