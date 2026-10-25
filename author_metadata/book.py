"""Book work-level title pages, distinct from praise and series furniture."""

import re

from .names import normalize_author_candidate, looks_like_person_name, split_author_line_candidates
from .title_evidence import matches, not_found, words

_OTHER_ROLE = re.compile(r"\b(?:series\s+editors?|volume\s+editors?|editorial\s+board|"
                         r"contributors?|translated\s+by|translation\s+by|foreword\s+by|"
                         r"introduction\s+by|with\s+an\s+introduction|afterword\s+by)\b", re.I)
_PRAISE = re.compile(r"^(?:praise\s+for|advance\s+praise|acclaim\s+for|reviews?\s+of|endorsements?)\b", re.I)
_IMPRINT = re.compile(r"\b(?:university\s+press|publishing|publishers|press|routledge|"
                      r"palgrave|macmillan|penguin|harpercollins|polity)\b", re.I)


def _names(line):
    if _OTHER_ROLE.search(line) or _IMPRINT.search(line) or any(mark in line for mark in ('"', '\u201c', '\u201d', ':', '\u2022')):
        return []
    found = split_author_line_candidates(line, require_complete=True)
    if found:
        if (re.search(r"\band\b", line, re.I) or "&" in line) and len(found) < 2:
            return []
        return found
    candidate = normalize_author_candidate(line)
    return [candidate] if looks_like_person_name(candidate) else []


def _title_matches(visible, hint, names):
    if re.search(r"\b(?:edited\s+by|written\s+by|foreword|copyright|contributors?|series\s+editors?)\b", visible, re.I):
        return False
    # DOS 8.3 filenames carry no usable work title. An explicit title-page
    # role can instead use the complete printed title immediately above it.
    if re.fullmatch(r"[A-Za-z]{2,6}~\d+", hint) and len(words(visible)) >= 3:
        return True
    if matches(visible, hint, names) or (len(words(hint)) >= 2 and words(visible) == words(hint)):
        return True
    parts = re.split(r"\s+--\s+", hint)
    if (len(parts) >= 3 and any(_IMPRINT.search(part) for part in parts[2:])
            and matches(visible, parts[0], names)):
        return True
    if len(parts) >= 2 and words(parts[1]) == words(" ".join(names)):
        if any(not (_IMPRINT.search(part) or re.fullmatch(r"\d{4}", part)) for part in parts[2:]):
            return False
        core = parts[0]
    else:
        core = hint
        suffix = re.search(r"\s+[-\u2013\u2014]\s*(.+)$", core)
        if suffix and words(suffix.group(1)) == words(" ".join(names)):
            core = core[:suffix.start()]
    # Book catalog labels can carry a series in trailing parentheses. A
    # complete printed work title remains mandatory before that annotation.
    core = re.sub(r"\s*\([^()]+\)\s*$", "", core)
    expected, observed = words(core), words(visible)
    return len(expected) >= 3 and observed[:len(expected)] == expected


def _copyright_rosters(samples):
    """Read the same explicit credits once before comparing candidate rosters."""
    rosters = set()
    for sample in samples:
        for line in sample["text"].splitlines():
            credit = re.match(r"^\s*Copyright\s*(?:\u00a9|\(c\))?\s*(?:\d{4}\s*)?(?:by\s+)?(.+)", line, re.I)
            if credit:
                raw = re.sub(r"\s*\d{4}\.?$", "", credit.group(1)).strip(" .")
                credited_names = _names(raw)
                rosters.add(tuple(tuple(words(name)) for name in credited_names))
    return rosters


def _copyright_corroborates(samples, names):
    return tuple(tuple(words(name)) for name in names) in _copyright_rosters(samples)


def _catalog_credit(context):
    """Keep explicit book-catalog authors as filename evidence, not a byline."""
    if context.path is None:
        return not_found()
    fields = re.split(r"\s+--\s+", context.path.stem)
    if len(fields) < 3 or not all(
        _IMPRINT.search(field) or re.fullmatch(r"(?:18|19|20)\d{2}", field)
        for field in fields[2:]
    ):
        return not_found()
    credit = re.sub(r"\b([A-Z])_(?=\s|$)", r"\1.", fields[1])
    if _OTHER_ROLE.search(credit):
        return not_found()
    names = _names(credit)
    if not names:
        return not_found()
    for sample in context.samples:
        lines = [" ".join(line.split()) for line in sample["text"].splitlines() if line.strip()]
        for index, line in enumerate(lines):
            if _OTHER_ROLE.search(line):
                role_words = words(" ".join(lines[index:index + 3]))
                if any(any(role_words[offset:offset + len(words(name))] == words(name)
                           for offset in range(len(role_words))) for name in names):
                    return not_found()
    core_title = re.sub(r"\s*\([^()]+\)\s*$", "", fields[0])
    expected = words(core_title)
    selected = words(re.sub(r"\s*\([^()]+\)\s*$", "", context.title_hint))
    main_title = words(re.split(r"\s+_\s+|:\s+", core_title, maxsplit=1)[0])
    if len(selected) < 2 or selected not in (expected, main_title):
        return not_found()
    if not any(
        any(words(line)[:len(selected)] == selected or
            words(re.sub(r"^praise\s+for\s+", "", line, flags=re.I))[:len(selected)] == selected
            for line in sample["text"].splitlines())
        for sample in context.samples if 1 <= int(sample["page"]) <= 4
    ):
        return not_found()
    for sample in context.samples:
        if not 1 <= int(sample["page"]) <= 4:
            continue
        lines = [" ".join(line.split()) for line in sample["text"].splitlines() if line.strip()]
        for line in lines[:3]:
            heading = re.fullmatch(r"praise\s+for\s+(.+?)\s+by\s+(.+)", line, flags=re.I)
            if not heading or words(heading.group(1)) not in (expected, selected):
                continue
            printed_names = _names(heading.group(2))
            if tuple(map(words, printed_names)) == tuple(map(words, names)):
                return {"author": ", ".join(printed_names), "source": "filename_corroborated_text_name",
                        "page": int(sample["page"]), "evidence": line + " / " + context.path.name}
    return {"author": ", ".join(names), "source": "filename_author_fallback",
            "page": 0, "evidence": context.path.name}


def _explicit_titlepage_credit(context, samples):
    """Read complete by/editor lists attached to this book's printed title.

    PDF title pages often put the role and two names on the same line, then
    continue the list on following lines. The older parser only accepted a
    standalone `edited by` line and stopped at the first inline name.
    """
    hint = re.split(r"\s+--\s+", context.title_hint)[0]
    hint_words = words(hint)
    if len(hint_words) < 3:
        return not_found()
    reports = []
    role = re.compile(r"^(?:(?:edited|written)\s+by|by)\s*:?[\s\u00ad]*(.*)$", re.I)
    for sample in samples:
        lines = [" ".join(line.replace("\u00ad", "").split())
                 for line in sample["text"].splitlines() if line.strip()]
        # Publisher series catalogs enumerate many neighboring books with
        # their creators. None is a title-page credit for this PDF.
        if sum(bool(role.fullmatch(value)) for value in lines[:32]) >= 3:
            continue
        for index, line in enumerate(lines[:22]):
            match = role.fullmatch(line)
            if not match or any(_PRAISE.match(v) for v in lines[:index]):
                continue
            preceding = words(" ".join(lines[max(0, index - 5):index]))
            # Require the document's own title directly above the credit.
            # Catalog filenames can truncate a title; their opening words
            # still have to agree with the printed work.
            overlap = min(len(hint_words), 4)
            # A damaged cover may leave only the printed subtitle legible.
            # Require a four-word contiguous phrase from the selected title;
            # this is still tied to the work, not any nearby person name.
            if not any(preceding[pos:pos + overlap] == hint_words[start:start + overlap]
                       for start in range(max(0, len(hint_words) - overlap + 1))
                       for pos in range(max(0, len(preceding) - overlap + 1))):
                continue
            credit = [match.group(1)] if match.group(1).strip() else []
            for next_line in lines[index + 1:index + 6]:
                if _IMPRINT.search(next_line) or _OTHER_ROLE.search(next_line):
                    break
                credit.append(next_line)
                joined = " ".join(credit).strip(" ,")
                if not joined or credit[-1].rstrip().endswith((",", "and", "AND", "&")):
                    continue
                names = split_author_line_candidates(joined, require_complete=True)
                if not names:
                    single = normalize_author_candidate(joined)
                    names = [single] if looks_like_person_name(single, title_hint=hint) else []
                if names and all(looks_like_person_name(name, title_hint=hint) for name in names):
                    reports.append({"author": ", ".join(names), "source": "text_strict_credit_block",
                                    "page": int(sample["page"]), "evidence": line + " / " + joined})
                    # A complete inline list ends here. Subsequent text can
                    # be a series subtitle or the opening chapter heading.
                    break
            # An inline credit may be complete without a following name line.
            if match.group(1).strip():
                names = split_author_line_candidates(match.group(1), require_complete=True)
                if not names:
                    name = normalize_author_candidate(match.group(1))
                    names = [name] if looks_like_person_name(name, title_hint=hint) else []
                if names:
                    reports.append({"author": ", ".join(names), "source": "text_strict_credit_block",
                                    "page": int(sample["page"]), "evidence": line})
    if not reports:
        return not_found()
    # The longest complete credit list is preferred; a shorter inline prefix
    # on the same page is just a partial rendering of that list.
    reports.sort(key=lambda row: len(row["author"].split(", ")), reverse=True)
    return reports[0]


def _frontmatter_credit(context, samples):
    """Use explicit author-rights or work-level library catalog records."""
    hint = re.split(r"\s+--\s+", context.title_hint)[0]
    expected = words(hint)
    reports = []
    printed_titlepages = []
    for row in samples:
        opening_lines = [" ".join(line.split()) for line in row["text"].splitlines() if line.strip()][:14]
        opening_words = words(" ".join(opening_lines))
        if len(expected) >= 3 and any(opening_words[pos:pos + 3] == expected[:3]
                                      for pos in range(max(0, len(opening_words) - 2))):
            printed_titlepages.append((int(row["page"]), opening_words))
    for sample in samples:
        lines = [" ".join(line.split()) for line in sample["text"].splitlines() if line.strip()]
        text = " ".join(lines[:35])
        if re.search(r"\b(?:Library of Congress Cataloging|British Library Cataloguing)", text, re.I):
            # Work-level CIP: title / author or editors. It is only accepted
            # when the title itself matches the selected work's opening words.
            catalog = re.search(r"(?:^|\s)(.{10,160}?)\s*/\s*(?:(?:edited|written)\s+by\s+)?"
                                r"(.{5,150}?)\.\s*(?:p\.\s*cm\.|pages\s+cm|Includes\b)",
                                text, re.I)
            if catalog and len(expected) >= 3:
                title_words = words(catalog.group(1))
                if any(title_words[pos:pos + 3] == expected[:3]
                       for pos in range(max(0, len(title_words) - 2))):
                    raw = re.sub(r",?\s+editors?\.?$", "", catalog.group(2), flags=re.I)
                    names = split_author_line_candidates(raw, require_complete=True)
                    if not names:
                        name = normalize_author_candidate(raw)
                        names = [name] if looks_like_person_name(name, title_hint=hint) else []
                    if names and all(looks_like_person_name(name, title_hint=hint) for name in names):
                        reports.append({"author": ", ".join(name.rstrip("." ) for name in names),
                                        "source": "text_bibliographic_author_label",
                                        "page": int(sample["page"]), "evidence": catalog.group(0)[:250]})
        # Copyright owner alone is insufficient. This exact rights clause
        # asserts authorship of the work, even when the title page is a scan.
        rights = re.search(r"\bThe rights? of (.{5,80}?) (?:as|to be identified as) (?:the )?author\b", text, re.I)
        if rights:
            name = normalize_author_candidate(rights.group(1))
            if looks_like_person_name(name):
                reports.append({"author": name, "source": "text_bibliographic_author_label",
                                "page": int(sample["page"]), "evidence": rights.group(0)})
        for copyright_line in lines[:25]:
            copyrighted = re.fullmatch(r"(?:Text copyright\s*:\s*|(?:Copyright\s*)?©\s*)"
                                      r"(?:18|19|20)\d{2}\s+(.{5,80}?)\.?", copyright_line, re.I)
            if not copyrighted:
                continue
            name = normalize_author_candidate(copyrighted.group(1))
            name_words = words(name)
            if not looks_like_person_name(name) or not name_words:
                continue
            if any(abs(int(sample["page"]) - page) <= 2 and any(
                page_words[pos:pos + len(name_words)] == name_words
                for pos in range(max(0, len(page_words) - len(name_words) + 1)))
                   for page, page_words in printed_titlepages):
                reports.append({"author": name, "source": "text_bibliographic_author_label",
                                "page": int(sample["page"]), "evidence": copyright_line})
    authors = {report["author"].casefold() for report in reports}
    return reports[0] if len(authors) == 1 else not_found()


def _embedded_chapter_credit(context, samples):
    """A chapter excerpt whose PDF begins with the edited parent frontmatter."""
    hint = words(re.split(r"\s+--\s+", context.title_hint)[0])
    if len(hint) < 4:
        return not_found()
    by_page = {int(sample["page"]): sample["text"] for sample in samples}
    if any(any(words(line)[:4] == hint[:4] for line in by_page.get(page, "").splitlines()[:8])
           for page in (1, 2)):
        return not_found()
    for page in range(3, 12):
        if page not in by_page or page + 1 not in by_page:
            continue
        lines = [" ".join(line.split()) for line in by_page[page].splitlines() if line.strip()][:12]
        next_lines = [" ".join(line.split()) for line in by_page[page + 1].splitlines() if line.strip()][:5]
        for index in range(len(lines) - 1):
            for width in (1, 2):
                if index + width >= len(lines):
                    continue
                title = " ".join(lines[index:index + width])
                if words(title)[:4] != hint[:4]:
                    continue
                name = normalize_author_candidate(lines[index + width])
                if not looks_like_person_name(name, title_hint=title):
                    continue
                if any(words(line) == words(name) for line in next_lines):
                    return {"author": name, "source": "text_strict_credit_block",
                            "page": page, "evidence": title + " / " + name + " / next-page running head"}
    return not_found()


def infer(context):
    if context.profile.kind != "book":
        return not_found()
    reports = []
    samples = tuple(sample for sample in context.samples if 1 <= int(sample["page"]) <= 12)
    embedded = _embedded_chapter_credit(context, samples)
    if embedded.get("author"):
        return embedded
    explicit_credit = _explicit_titlepage_credit(context, samples)
    frontmatter_credit = _frontmatter_credit(context, samples)
    copyright_rosters = _copyright_rosters(samples)
    for sample in samples:
        lines = [" ".join(line.split()) for line in sample["text"].splitlines() if line.strip()][:32]
        if any(_PRAISE.match(line) for line in lines[:6]):
            continue
        # Some PDFs emit title-page drawing objects in reverse block order:
        # imprint, names, role, title. The role still bounds the complete name
        # block, and the complete work title must follow it directly.
        for role_index, line in enumerate(lines[:18]):
            if not re.fullmatch(r"(?:by|written\s+by|edited\s+by)\s*:?", line, re.I):
                continue
            if any(_OTHER_ROLE.search(value) for value in lines[:role_index]):
                continue
            reverse_reports = []
            for width in range(1, min(6, role_index) + 1):
                block = lines[role_index - width:role_index]
                if any(_IMPRINT.search(value) for value in block):
                    continue
                names = _names(" ".join(block))
                if not names:
                    continue
                for title_width in range(1, min(6, len(lines) - role_index)):
                    title = " ".join(lines[role_index + 1:role_index + 1 + title_width])
                    if (_title_matches(title, context.title_hint, names)
                            and all(looks_like_person_name(name, title_hint=title) for name in names)):
                        reverse_reports.append((len(names), width, {"author": ", ".join(names), "source": "text_strict_credit_block",
                                        "page": int(sample["page"]), "evidence": " / ".join(block + [line])}))
            if reverse_reports:
                reports.append(max(reverse_reports, key=lambda item: item[:2])[2])
        consumed_credit_until = 0
        for start in range(min(18, len(lines))):
            if start < consumed_credit_until:
                continue
            explicit = re.fullmatch(r"(?:by|written\s+by|edited\s+by)\s*:?", lines[start], re.I)
            inline = re.fullmatch(r"(?:by|written\s+by|edited\s+by)\s+(.+)", lines[start], re.I)
            if start and not (explicit or inline) and _names(lines[start - 1]):
                current_names = _names(lines[start])
                matched_title_above = current_names and any(
                    _title_matches(" ".join(lines[start - width:start]), context.title_hint, current_names)
                    for width in range(1, min(start, 5) + 1)
                )
                if not matched_title_above:
                    continue
            cursor = start + 1 if explicit else start
            if any(_OTHER_ROLE.search(line) for line in lines[max(0, start - 2):start + 1]):
                continue
            names, credit = [], []
            while cursor < min(len(lines), start + 7):
                raw = inline.group(1) if inline and cursor == start else lines[cursor]
                if names:
                    raw = re.sub(r"^(?:and|&)\s+", "", raw, count=1, flags=re.I)
                found = _names(raw)
                if not found:
                    break
                names.extend(name for name in found if name not in names)
                credit.append(lines[cursor])
                cursor += 1
            if not names:
                continue
            # Bare title-page names need a nearby publisher imprint. Explicit
            # work-level authors/editors need the matched work title instead.
            publisher_backed = any(_IMPRINT.search(line) for line in lines[cursor:cursor + 3])
            copyright_backed = (
                tuple(tuple(words(name)) for name in names) in copyright_rosters
            )
            if not (explicit or inline) and not publisher_backed and not copyright_backed:
                continue
            for width in range(1, min(start, 5) + 1):
                title = " ".join(lines[start - width:start])
                if not _title_matches(title, context.title_hint, names):
                    continue
                if not all(looks_like_person_name(name, title_hint=title) for name in names):
                    continue
                evidence = credit if not explicit else [lines[start], *credit]
                if copyright_backed and not publisher_backed:
                    evidence = [*evidence, "matching copyright credit"]
                reports.append({"author": ", ".join(names), "source": "text_strict_credit_block" if explicit or inline or not publisher_backed else "text_titlepage_publisher_byline",
                                "page": int(sample["page"]), "evidence": " / ".join(evidence)})
                consumed_credit_until = cursor
    # Conflicting work-level title-page credits are ambiguity, not a reason
    # to privilege whichever page happened to be sampled first.
    unique = {report["author"] for report in reports}
    if len(unique) == 1:
        if (not explicit_credit.get("author") or
                len(reports[0]["author"].split(", ")) >= len(explicit_credit["author"].split(", "))):
            return reports[0]
        return explicit_credit
    if unique:
        return {"author": "", "source": "conflicting_work_credits", "page": 0,
                "evidence": "; ".join(f"page {row['page']}: {row['author']}" for row in reports)}
    if explicit_credit.get("author"):
        return explicit_credit
    if frontmatter_credit.get("author"):
        return frontmatter_credit
    return _catalog_credit(context)
