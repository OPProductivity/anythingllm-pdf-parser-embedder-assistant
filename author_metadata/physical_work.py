"""A bounded title hypothesis from an opening's actual credit structure."""

import re
from .structured_credit import _names
from .title_evidence import words


def opening_title(lines):
    """Only an opening catalog record or title/name/introduction sequence.

    No title dictionary, expected labels, filename or PDF Author is used.
    """
    lines = [line.strip() for line in lines if line.strip()]
    if len(lines) < 4:
        return ""
    for index in range(min(3, len(lines) - 2)):
        title, credit = lines[index : index + 2]
        if not 3 <= len(words(title)) <= 24 or not _names(credit):
            continue
        if re.search(
            r"(?i)\b(?:edited|translated|foreword|review|contents)\b",
            title + " " + credit,
        ):
            continue
        tail = "\n".join(lines[index + 2 : index + 10])
        catalog = bool(
            re.search(r"(?i)\b(?:vol\.|source:)\s*\d*", tail)
            and re.search(r"(?i)stable url|jstor\.org", tail)
        )
        introduction = bool(
            re.match(
                r"(?i)^(?:[IVX]+\s*)?(?:introduction|abstract)\b",
                " ".join(lines[index + 2 : index + 4]),
            )
        )
        if catalog or introduction:
            return title
    return ""


def opening_title_hypotheses(bundle, title_hint="", *, work_scope="single_work"):
    """Title-page structure can supply a work hypothesis without PDF Info.

    This does not declare any nearby person an author. Sparse title pages,
    responsibility blocks and imprint/edition apparatus jointly justify an
    alternative work title for all specialists to assess.
    """
    if work_scope == "whole_issue":
        return []
    native = [p for p in bundle.get("native", []) if p.get("page", 0) <= 5]
    hint = words(title_hint)
    visible = words(" ".join(p.get("text", "") for p in native))
    width = min(4, len(hint))
    if width >= 2 and any(
        visible[i : i + width] == hint[:width]
        for i in range(max(0, len(visible) - width + 1))
    ):
        return []
    pages = [(p, p.get("horizontal_lines", []), "native") for p in native]
    for region in bundle.get("ocr", []):
        if region.get("page", 0) > 5:
            continue
        lines = [
            {**line, "size": max(1, line["box"][3] - line["box"][1])}
            for line in region.get("lines", [])
        ]
        pages.append((region, lines, "ocr"))
    found = {}
    exclude = re.compile(
        r"\b(?:contents|praise|endorsements|foreword|preface|acknowledg\w*|references|bibliography|index|copyright|reviewed)\b",
        re.I,
    )
    for page, lines, representation in pages:
        text = "\n".join(line["text"] for line in lines)
        if (
            not lines
            or len(text.split()) > 180
            or exclude.search(" ".join(line["text"] for line in lines[:3]))
        ):
            continue
        if work_scope == "chapter" and not re.search(
            r"(?im)^\s*(?:chapter\s*)?\d{1,3}[.:]?\s+", text
        ):
            continue
        acts = list(
            re.finditer(r"(?im)^\s*(?:(?:edited|written|translated)\s+by|by)\b", text)
        )
        if len(acts) > 3 or re.search(r"\ba series edited by\b", text, re.I):
            continue
        role_lines = [
            line
            for line in lines
            if re.match(r"^(?:(?:edited|written)\s+by|by)\b", line["text"], re.I)
        ]
        title_zone = (
            [line for line in lines if line["box"][3] <= role_lines[0]["box"][1] + 2]
            if role_lines
            else lines
        )
        candidates = [
            line
            for line in title_zone
            if 1 <= len(words(line["text"])) <= 24
            and not exclude.search(line["text"])
            and not re.search(
                r"\b(?:edition|press|publishers?|edited|translated)\b",
                line["text"],
                re.I,
            )
        ]
        if not candidates:
            continue
        largest = max(line["size"] for line in candidates)
        heading = [line for line in candidates if line["size"] >= largest * 0.9]
        # A named creator cannot justify itself as the title. The title block
        # must be followed by a distinct responsibility/name band.
        bottom = max(line["box"][3] for line in heading)
        later = [line for line in lines if line["box"][1] >= bottom - 2]
        title = " ".join(line["text"] for line in heading)
        if not 2 <= len(words(title)) <= 45:
            continue
        explicit = any(
            re.match(r"^(?:(?:edited|written)\s+by|by)\b", line["text"], re.I)
            for line in later
        )
        people = [
            line
            for line in later
            if (explicit or line["size"] < largest * 0.95) and _names(line["text"])
        ]
        imprint = bool(
            re.search(r"\b(?:press|publishers?|publishing|edition|ISBN)\b", text, re.I)
        ) or any(
            re.fullmatch(
                r"[A-Z][A-Za-z .’'-]{1,35}\s*[•·]\s*[A-Z][A-Za-z .’'-]{1,35}",
                line["text"],
            )
            for line in later
        )
        if not people or not (explicit or imprint):
            continue
        key = tuple(words(title))
        found.setdefault(
            key,
            {
                "title": title,
                "page": page["page"],
                "representation": representation,
                "mechanism": "sparse_titlepage_responsibility_structure",
                "basis": "larger title, distinct responsibility/name band, and explicit responsibility or imprint/edition; an author verdict still requires separate expert assessment",
            },
        )
    return list(found.values())
