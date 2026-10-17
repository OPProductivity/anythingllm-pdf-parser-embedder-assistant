"""Bounded work-title/byline relations, independent of publisher or person names."""

import re
import unicodedata

from .names import looks_like_person_name, normalize_author_candidate
from .title_evidence import not_found


def _words(value):
    value = "".join(
        c
        for c in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(c)
    )
    return tuple(re.findall(r"[^\W\d_]+", value))


def _title_supported(visible, hint, names):
    observed, expected = _words(visible), _words(hint)
    if len(observed) < 3:
        return False
    person_words = set(_words(" ".join(names)))
    expected = tuple(w for w in expected if w not in person_words)
    if len(expected) < 3:
        return False
    if observed == expected or observed[: len(expected)] == expected:
        return True
    # A selected title can be clipped in its final word by a PDF exporter.
    return (
        len(expected) >= 5
        and len(expected[-1]) >= 4
        and len(observed) >= len(expected)
        and observed[: len(expected) - 1] == expected[:-1]
        and observed[len(expected) - 1].startswith(expected[-1])
    )


def _names(value):
    if re.match(r"^\s*(?:coordinated\s+by|contributing\s+authors?\s*:)", value, re.I):
        return []
    value = re.sub(
        r"^(?:by|authors?\s*:|Dr\.|Prof\.|Ms\.|Mr\.|Mrs\.)\s*", "", value, flags=re.I
    )
    value = re.sub(r"[\d*†‡§]+$", "", value).strip()
    if "/" in value:
        left, right = value.split("/", 1)
        if re.search(r"\b(?:university|college|institute|department)\b", right, re.I):
            value = left.strip()
    if re.search(r"\b(?:university|college|institute|department|tech)\b", value, re.I):
        return []
    # A terminal comma is punctuation, while a comma before Jr./Sr. belongs
    # to one person. Neither should erase the complete roster.
    terminal_separator = bool(re.search(r"[,;]\s*$", value))
    value = value.strip().rstrip(",; ")
    value = re.sub(
        r",\s*(Jr\.|Sr\.)(?=\s*(?:[,;]|and\b|&|$))", r" \1", value, flags=re.I
    )
    value = re.sub(r",\s*(?:and|&)\s+", ", ", value, flags=re.I)
    parts = re.split(r"\s+(?:and|&)\s+|\s*[;·•]\s*|\s*,\s*", value, flags=re.I)
    if terminal_separator and len(parts) > 1:
        # A multi-person line ending in a separator may continue on the next
        # line. Its prefix is not a complete roster. A single byline followed
        # by affiliation can still carry terminal punctuation.
        return []
    names = [normalize_author_candidate(p) for p in parts]
    names = [re.sub(r"\s+(Jr\.|Sr\.)$", r", \1", name, flags=re.I) for name in names]
    return (
        names
        if names and all(looks_like_person_name(p, allow_all_caps=True) for p in names)
        else []
    )


def credential_roster_reports(context):
    if context.profile.kind != "scholarly_article":
        return
    for sample in context.opening_pages:
        if int(sample["page"]) != 1:
            continue
        lines = [" ".join(v.split()) for v in sample["text"].splitlines() if v.strip()]
        names, evidence = ([], [])
        for index, line in enumerate(lines[:80]):
            credit = re.fullmatch(
                "(?:(?:Dr\\.|Prof\\.|Ms\\.|Mr\\.|Mrs\\.)\\s+)?([^,]{5,90}),\\s*(.+)",
                line,
                re.I,
            )
            if not credit or not re.search(
                "\\b(?:university|college|institute|tech)\\b", credit.group(2), re.I
            ):
                continue
            if len(credit.group(2)) > 80 or re.search(
                "^(?:is|was|has|received|holds)\\b", credit.group(2), re.I
            ):
                continue
            found = _names(credit.group(1))
            if not found:
                continue
            if not names and (
                not any(
                    (
                        _title_supported(
                            " ".join(lines[start:index]), context.title_hint, found
                        )
                        for start in range(max(0, index - 5), index)
                    )
                )
            ):
                continue
            names.extend(found)
            evidence.append(line)
        if len(names) >= 2:
            yield {
                "author": ", ".join(dict.fromkeys(names)),
                "source": "text_credential_author_roster",
                "page": 1,
                "evidence": " / ".join(evidence),
            }
    return


def credential_roster(context):
    reports = list(credential_roster_reports(context))
    return reports[0] if reports else not_found()


def infer_reports(context):
    if (
        context.profile.kind not in {"scholarly_article", "generic"}
        or context.profile.work_scope == "whole_issue"
    ):
        return
    frontmatter = any(
        (
            re.search("\\bcontents\\b", s["text"][:800], re.I)
            for s in context.opening_pages
        )
    )
    planned = (
        {int(s["page"]) for s in context.planned_opening_pages}
        if (context.evidence_bundle or {}).get("trace", {}).get("mode")
        != "supplied_samples_only"
        and context.evidence_bundle is not None
        else None
    )
    for sample in context.samples:
        if (
            int(sample["page"]) not in planned
            if planned is not None
            else int(sample["page"]) > (12 if frontmatter else 5)
        ):
            continue
        lines = [
            " ".join(unicodedata.normalize("NFKC", v).split())
            for v in sample["text"].splitlines()
            if v.strip()
        ]
        for index in range(1, min(14, len(lines) - 1)):
            candidates = [(lines[index], index + 1)]
            if index + 1 < len(lines) and re.search("\\b[A-Z]\\.\\s*$", lines[index]):
                candidates.append((lines[index] + " " + lines[index + 1], index + 2))
            for value, following in candidates:
                names = _names(value)
                if not names:
                    continue
                previous = _words(lines[index - 1])
                expected = _words(context.title_hint)
                if _names(lines[index - 1]) and (
                    not any(
                        (
                            expected[n : n + len(previous)] == previous
                            for n in range(len(expected))
                        )
                    )
                ):
                    continue
                citation = next(
                    (
                        n
                        for n in range(following, min(len(lines), following + 3))
                        if re.match("^To cite this article:", lines[n], re.I)
                    ),
                    None,
                )
                cited_title = None
                if citation is not None:
                    printed = re.match(
                        "^To cite this article:\\s*(.*?)\\s*\\(\\d{4}\\)\\s*:?\\s*(.*)",
                        " ".join(lines[citation : citation + 6]),
                        re.I,
                    )
                    if not printed or [_words(n) for n in _names(printed.group(1))] != [
                        _words(n) for n in names
                    ]:
                        continue
                    cited_title = _words(printed.group(2))
                if any(
                    (
                        re.search(
                            "\\b(?:reviewed by|series editor|interview|contents|references|bibliography)\\b",
                            s,
                            re.I,
                        )
                        for s in lines[:index]
                    )
                ):
                    continue
                title_ok = any(
                    (
                        _title_supported(
                            " ".join(lines[start:index]), context.title_hint, names
                        )
                        and (
                            cited_title is None
                            or cited_title[: len(_words(" ".join(lines[start:index])))]
                            == _words(" ".join(lines[start:index]))
                            or cited_title[: len(_words(context.title_hint))]
                            == _words(context.title_hint)
                        )
                        for start in range(max(0, index - 6), index)
                    )
                )
                if not title_ok:
                    continue
                next_text = " ".join(lines[following : following + 3])
                publication_header = bool(
                    re.search(
                        "Published on:|License: Creative Commons", next_text, re.I
                    )
                )
                # A delayed title page can end in a matching personal
                # copyright credit without article prose or an abstract.
                copyright_match = re.search(
                    r"(?i)(?:copyright|©)\s*(?:©\s*)?(?:\d{4}\s*)?(?:by\s+)?(.+)",
                    next_text,
                )
                copyright_names = (
                    _names(copyright_match[1].strip()) if copyright_match else []
                )
                publication_header = publication_header or bool(
                    copyright_names
                    and _words(", ".join(copyright_names)) == _words(", ".join(names))
                )
                if not (
                    publication_header
                    or len(next_text.split()) >= 14
                    or re.match("^\\d+\\s+[A-Z]", next_text)
                    or re.search(
                        "\\b(?:abstract|university|college|department)\\b",
                        next_text,
                        re.I,
                    )
                ):
                    continue
                yield {
                    "author": ", ".join(names),
                    "source": "text_structured_opening_credit",
                    "page": int(sample["page"]),
                    "evidence": " / ".join(lines[max(0, index - 4) : following]),
                }
    return


def infer(context):
    reports = list(infer_reports(context))
    return reports[0] if reports else not_found()


def work_rights_credit_reports(context):
    if context.profile.kind not in {"generic", "book"}:
        return
    for sample in context.samples:
        text = " ".join(sample["text"].split())
        rights = re.search(
            "The right of (.{5,100}?) to be identified as (?:the )?author of this work",
            text,
            re.I,
        )
        names = _names(rights.group(1)) if rights else []
        if names:
            for titlepage in context.samples:
                if titlepage not in context.planned_opening_pages:
                    continue
                lines = [
                    " ".join(v.split())
                    for v in titlepage["text"].splitlines()
                    if v.strip()
                ]
                for index, line in enumerate(lines):
                    if _words(line) == _words(", ".join(names)) and any(
                        (
                            _title_supported(
                                " ".join(lines[start:index]), context.title_hint, names
                            )
                            for start in range(max(0, index - 5), index)
                        )
                    ):
                        yield {
                            "author": ", ".join(names),
                            "source": "text_work_rights_credit",
                            "page": int(titlepage["page"]),
                            "evidence": rights.group(0),
                            "corroboration_page": int(sample["page"]),
                        }
        copyright_credit = re.search(
            "(.{4,100}?)\\.\\s*Copyright\\s*©\\s*\\d{4}\\s+by\\s+(.{5,100}?)\\.\\s*All rights reserved",
            text,
            re.I,
        )
        if copyright_credit:
            names = _names(copyright_credit.group(2))
            if names and _words(
                copyright_credit.group(1).removeprefix("Copyright ")
            ) == _words(context.title_hint):
                yield {
                    "author": ", ".join(names),
                    "source": "text_work_rights_credit",
                    "page": int(sample["page"]),
                    "evidence": copyright_credit.group(0),
                }
    return


def work_rights_credit(context):
    reports = list(work_rights_credit_reports(context))
    return reports[0] if reports else not_found()


def positioned_opening_reports(context):
    """An unusable internal title must not hide a clean title/byline hierarchy."""
    if context.path is None or context.profile.kind not in {
        "scholarly_article",
        "generic",
        "book_chapter",
    }:
        return
    import fitz

    try:
        with fitz.open(context.path) as doc:
            pages = [
                (
                    p + 1,
                    sorted(
                        [b for b in doc[p].get_text("blocks") if b[6] == 0],
                        key=lambda b: (b[1], b[0]),
                    ),
                    doc[p].rect.height,
                )
                for p in range(min(2, len(doc)))
            ]
        for page, blocks, height in pages:
            if page == 1 and len(blocks) >= 3:
                first = blocks[0]
                names = _names(" ".join(first[4].split()))
                if names and first[1] < 80 and (first[3] - first[1] < 30):
                    for end in range(2, min(5, len(blocks))):
                        heading = " ".join((b[4] for b in blocks[1:end]))
                        body = blocks[end]
                        if (
                            blocks[end - 1][3] < height * 0.5
                            and body[1] > blocks[end - 1][3] + 8
                            and (len(body[4].split()) >= 60)
                            and _title_supported(heading, context.title_hint, names)
                            and (
                                not re.search(
                                    "\\b(?:review|interview|contents|editor)\\b",
                                    heading,
                                    re.I,
                                )
                            )
                        ):
                            yield {
                                "author": ", ".join(names),
                                "source": "text_structured_opening_credit",
                                "page": page,
                                "evidence": "opening byline above complete selected heading: "
                                + first[4].strip()
                                + " / "
                                + " ".join(heading.split()),
                            }
            for index, block in enumerate(blocks[:5]):
                title = " ".join(block[4].split())
                if (
                    len(_words(title)) < 3
                    or block[3] > height * 0.55
                    or index + 2 >= len(blocks)
                ):
                    continue
                credit, after = blocks[index + 1 : index + 3]
                names = _names(" ".join(credit[4].split()))
                if not names or not 3 <= credit[1] - block[3] <= 75:
                    continue
                aligned = (
                    abs(credit[0] - block[0]) <= 15
                    or abs(credit[0] + credit[2] - (block[0] + block[2])) <= 30
                )
                if not aligned or abs(after[0] - block[0]) > 35:
                    continue
                if (
                    not (
                        re.match("^\\s*\\d+\\s+[A-Z]", after[4])
                        or len(after[4].split()) >= 20
                    )
                    or credit[3] - credit[1] > 30
                ):
                    continue
                if block[3] - block[1] <= credit[3] - credit[1]:
                    continue
                if any(
                    (
                        re.search(
                            "\\b(?:review|interview|contents|editor)\\b", b[4], re.I
                        )
                        for b in blocks[: index + 1]
                    )
                ):
                    continue
                yield {
                    "author": ", ".join(names),
                    "source": "text_structured_opening_credit",
                    "page": page,
                    "evidence": title + " / " + " ".join(credit[4].split()),
                    "geometry": {"title": list(block[:4]), "byline": list(credit[:4])},
                }
    except OSError, ValueError, RuntimeError:
        pass
    return


def positioned_opening(context):
    reports = list(positioned_opening_reports(context))
    return reports[0] if reports else not_found()


def document_record_reports(context):
    """A wrapper's explicitly labelled chapter creator is not its book editor."""
    for sample in context.opening_pages:
        if int(sample["page"]) != 1:
            continue
        text = sample["text"]
        if all(
            (
                re.search(pattern, text, re.I)
                for pattern in [
                    "Chapter Title:",
                    "Book Title:",
                    "Stable URL:",
                    "Published by:",
                ]
            )
        ):
            credit = re.search("(?im)^Chapter Author\\(s\\):\\s*([^\\n]+)", text)
            names = _names(credit.group(1)) if credit else []
            if names:
                yield {
                    "author": ", ".join(names),
                    "source": "text_structured_opening_credit",
                    "page": 1,
                    "evidence": credit.group(0),
                }
        reported = re.search(
            "(?im)^Reported by\\s+(.{5,80}?)\\s*\\(\\s*Staff Writer\\s*\\)", text
        )
        if reported and re.search(
            "\\b(?:NAME OF WORKER|Circumstances of Interview)\\b", text, re.I
        ):
            names = _names(reported.group(1))
            if names:
                yield {
                    "author": ", ".join(names),
                    "source": "text_structured_opening_credit",
                    "page": 1,
                    "evidence": reported.group(0),
                }
        student = re.search("(?im)^\\s*(?:Student\\s+)?Name:\\s*([^\\n]+)", text)
        if student and re.search("\\bDiscussion Board Posts\\b", text, re.I):
            names = _names(student.group(1))
            if names:
                yield {
                    "author": ", ".join(names),
                    "source": "text_structured_opening_credit",
                    "page": 1,
                    "evidence": student.group(0),
                }
    return


def document_record(context):
    reports = list(document_record_reports(context))
    return reports[0] if reports else not_found()


def publisher_titlepage_reports(context):
    if context.path is None or context.profile.kind not in {"generic", "book"}:
        return
    import fitz

    try:
        with fitz.open(context.path) as doc:
            for sample in context.samples:
                page_number = int(sample["page"])
                if not 1 <= page_number <= min(8, len(doc)):
                    continue
                if len(sample["text"].split()) > 120:
                    continue
                page = doc[page_number - 1]
                lines = []
                for block in page.get_text(
                    "dict", flags=fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES
                )["blocks"]:
                    for line in block.get("lines", []):
                        text = " ".join(
                            "".join((s["text"] for s in line["spans"])).split()
                        )
                        if text:
                            lines.append(
                                (
                                    line["bbox"],
                                    text,
                                    max((s["size"] for s in line["spans"])),
                                )
                            )
                lines.sort(key=lambda v: (v[0][1], v[0][0]))
                if not 3 <= len(lines) <= 22:
                    continue
                for index in range(2, len(lines)):
                    if not re.search(
                        "\\b(?:university(?:\\s+of)?(?:\\s+[A-Za-z]+){0,5}\\s+press|publishing|publishers)\\b",
                        lines[index][1],
                        re.I,
                    ):
                        continue
                    last = index - 1
                    names = _names(lines[last][1])
                    first = last
                    if not names:
                        continue
                    while (
                        first > 1
                        and abs(lines[first - 1][2] - lines[last][2]) < 0.2
                        and _names(lines[first - 1][1])
                    ):
                        names = _names(lines[first - 1][1]) + names
                        first -= 1
                    role = next(
                        (
                            n
                            for n in range(index)
                            if re.fullmatch("edited by", lines[n][1], re.I)
                        ),
                        None,
                    )
                    if role is not None:
                        complete = []
                        for entry in lines[role + 1 : index]:
                            if re.fullmatch("and|&", entry[1], re.I):
                                continue
                            found = _names(entry[1].rstrip(","))
                            if not found:
                                complete = []
                                break
                            complete.extend(found)
                        if not complete:
                            continue
                        names = complete
                        first = role
                    title = " ".join((v[1] for v in lines[:first]))
                    if len(_words(title)) < 3 or not any(
                        (v[2] > lines[last][2] * 1.15 for v in lines[:first])
                    ):
                        continue
                    if re.search(
                        "\\b(?:series editor|translated by|foreword by|contents)\\b",
                        title,
                        re.I,
                    ):
                        continue
                    yield {
                        "author": ", ".join(names),
                        "source": "text_positioned_publisher_credit",
                        "page": page_number,
                        "evidence": title
                        + " / "
                        + ", ".join(names)
                        + " / "
                        + lines[index][1],
                    }
    except OSError, ValueError, RuntimeError:
        pass
    return


def publisher_titlepage(context):
    reports = list(publisher_titlepage_reports(context))
    return reports[0] if reports else not_found()


def remove_translator_role(report, context):
    if not report.get("author"):
        return report
    names = _names(report["author"])
    if len(names) < 2:
        return report
    text = "\n".join(s["text"] for s in context.samples)
    translated = re.findall(r"(?im)^\s*translated by\s*:?\s*([^\n]+)", text)
    excluded = {"".join(_words(n)) for line in translated for n in _names(line)}
    kept = [n for n in names if "".join(_words(n)) not in excluded]
    if kept and len(kept) < len(names):
        return {
            **report,
            "author": ", ".join(kept),
            "excluded_translator_credits": [n for n in names if n not in kept],
        }
    return report


def biographic_titlepage_reports(context):
    """Cross-check a compact multi-author title page against author biographies."""
    if context.profile.kind not in {"book", "generic"}:
        return
    biographies = [
        s["text"]
        for s in context.samples
        if 1 <= int(s["page"]) <= 4 and re.search("\\bThe Authors?\\b", s["text"], re.I)
    ]
    if not biographies:
        return
    bio_words = _words(" ".join(biographies))
    for sample in context.samples:
        if not 1 <= int(sample["page"]) <= 6:
            continue
        lines = [" ".join(v.split()) for v in sample["text"].splitlines() if v.strip()]
        if not 2 <= len(lines) <= 12:
            continue
        for index, line in enumerate(lines[1:], 1):
            names = _names(line)
            if len(names) < 2:
                continue
            if all(
                (
                    any(
                        (
                            bio_words[pos : pos + len(_words(name)) + 1]
                            == (*_words(name), "is")
                            for pos in range(len(bio_words))
                        )
                    )
                    for name in names
                )
            ):
                yield {
                    "author": ", ".join(names),
                    "source": "text_structured_opening_credit",
                    "page": int(sample["page"]),
                    "evidence": " / ".join(lines[: index + 1]),
                    "corroboration": "each title-page author has an explicit opening-page biography",
                }
    return


def biographic_titlepage(context):
    reports = list(biographic_titlepage_reports(context))
    return reports[0] if reports else not_found()


def whole_book_editors_reports(context):
    """An explicit title-page editor list outranks a misleading chapter filename."""
    if (
        context.path is None
        or context.profile.kind not in {"generic", "book"}
        or context.profile.work_scope in {"chapter", "whole_issue"}
    ):
        return
    for sample in context.samples:
        if not 1 <= int(sample["page"]) <= 8:
            continue
        lines = [" ".join(v.split()) for v in sample["text"].splitlines() if v.strip()]
        if len(lines) > 24:
            continue
        for index, line in enumerate(lines):
            suffix = re.fullmatch("(.{10,160}),\\s*editors?\\.?", line, re.I)
            suffix_names = _names(suffix.group(1)) if suffix else []
            if len(suffix_names) >= 2 and _title_supported(
                " ".join(lines[:index]), context.title_hint, suffix_names
            ):
                yield {
                    "author": ", ".join(suffix_names),
                    "source": "text_structured_opening_credit",
                    "page": int(sample["page"]),
                    "role": "editors",
                    "evidence": "whole-book title-page editor credit: " + line,
                }
            if (
                not re.fullmatch("edited by", line, re.I)
                or len(_words(" ".join(lines[:index]))) < 3
            ):
                continue
            names = []
            for value in lines[index + 1 :]:
                if re.fullmatch("and|&", value, re.I):
                    continue
                found = _names(value.rstrip(","))
                if not found:
                    break
                names.extend(found)
            if len(names) >= 2 and _title_supported(
                " ".join(lines[:index]), context.title_hint, names
            ):
                yield {
                    "author": ", ".join(names),
                    "source": "text_structured_opening_credit",
                    "page": int(sample["page"]),
                    "evidence": "whole-book title page: "
                    + " / ".join(lines[: index + 2]),
                    "role": "editors",
                }
    return


def whole_book_editors(context):
    reports = list(whole_book_editors_reports(context))
    return reports[0] if reports else not_found()


def continued_byline_reports(context):
    if context.profile.kind not in {"generic", "book_chapter", "scholarly_article"}:
        return
    first = next((s for s in context.samples if int(s["page"]) == 1), None)
    if first is None:
        return
    lines = [" ".join(v.split()) for v in first["text"].splitlines() if v.strip()]
    for line in lines[:12]:
        partial = re.fullmatch("(.{5,90})\\s+and", line, re.I)
        names = _names(partial.group(1)) if partial else []
        if not names:
            continue
        for sample in context.samples:
            if not 2 <= int(sample["page"]) <= 3:
                continue
            leading = [v for v in sample["text"].splitlines() if v.strip()][:6]
            for header in leading:
                header = re.sub("^\\s*\\d+\\s*[•|·—–-]?\\s*", "", header)
                complete = _names(header)
                if len(complete) > len(names) and all(
                    (_words(n) in [_words(v) for v in complete] for n in names)
                ):
                    yield {
                        "author": ", ".join(complete),
                        "source": "text_structured_opening_credit",
                        "page": 1,
                        "evidence": line + " / " + header,
                        "corroboration_page": int(sample["page"]),
                    }
    return


def continued_byline(context):
    reports = list(continued_byline_reports(context))
    return reports[0] if reports else not_found()
