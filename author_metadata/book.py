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
        return found
    candidate = normalize_author_candidate(line)
    return [candidate] if looks_like_person_name(candidate) else []


def _title_matches(visible, hint, names):
    if re.search(r"\b(?:edited\s+by|written\s+by|foreword|copyright|contributors?|series\s+editors?)\b", visible, re.I):
        return False
    if matches(visible, hint, names) or (len(words(hint)) >= 2 and words(visible) == words(hint)):
        return True
    parts = re.split(r"\s+--\s+", hint)
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


def _copyright_corroborates(samples, names):
    name_words = tuple(words(name) for name in names)
    for sample in samples:
        for line in sample["text"].splitlines():
            credit = re.match(r"^\s*Copyright\s*(?:\u00a9|\(c\))?\s*(?:\d{4}\s*)?(?:by\s+)?(.+)", line, re.I)
            if credit:
                raw = re.sub(r"\s*\d{4}\.?$", "", credit.group(1)).strip(" .")
                credited_names = _names(raw)
                if tuple(words(name) for name in credited_names) == name_words:
                    return True
    return False


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


def infer(context):
    if context.profile.kind != "book":
        return not_found()
    reports = []
    samples = tuple(sample for sample in context.samples if 1 <= int(sample["page"]) <= 12)
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
            copyright_backed = _copyright_corroborates(samples, names)
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
        return reports[0]
    return not_found() if unique else _catalog_credit(context)
