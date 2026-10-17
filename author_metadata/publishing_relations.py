"""Publishing acts are relations, with wording families and occurrence scope.

This is a conservative rule model, not trained probabilities. Region labels
remain descriptive; an explicit work responsibility can outweigh their location.
"""

import re
from .structured_credit import _names, _words

# Different surface forms express the same role assertion. Mere copyright
# ownership, permissions, and a translator's moral rights are not equivalent.
_RIGHTS_FAMILIES = (
    (
        "identification_right",
        r"\b(?:the\s+)?rights?\s+of\s+(.{5,180}?)\s+to\s+be\s+identified\s+as\s+(?:the\s+)?authors?\s+of\s+(?:this|the)\s+(?:work|book)",
    ),
    (
        "author_rights_asserted",
        r"\b(?:the\s+)?rights?\s+of\s+(.{5,180}?)\s+as\s+(?:the\s+)?authors?\s+(?:has|have)\s+been\s+asserted",
    ),
    (
        "person_asserts_moral_right",
        r"(?<!\w)([A-ZÀ-ÖØ-Þ][^!?;©]{4,150}?)\s+(?:has|have)\s+asserted\s+(?:(?:his|her|their|the)\s+)?(?:moral\s+)?rights?\s+to\s+be\s+identified\s+as\s+(?:the\s+)?authors?\s+of\s+(?:this|the)\s+(?:work|book)",
    ),
    (
        "moral_rights_as_author",
        r"\bmoral\s+rights?\s+of\s+(.{5,180}?)\s+as\s+(?:the\s+)?authors?\s+(?:has|have)\s+been\s+asserted",
    ),
)
_SECTION_FAMILIES = {
    "contents": r"^(?:table of contents|contents|contents at a glance|sum[aá]rio|inhoud(?:sopgave)?)$",
    "index": r"^(?:index|name index|subject index|author index|[ií]ndice)$",
    "praise": r"^(?:praise(?: for.*?)?|endorsements|advance praise|what (?:readers|reviewers) (?:say|are saying)|reviews of.*?)$",
    "foreword": r"^(?:foreword|preface to .*?edition|with (?:a|an) (?:foreword|introduction) by.*?)\s*:?",
    "dedication": r"^(?:dedication|dedicated to|this (?:book|work) is dedicated to|for my|to my)\b",
    "abstract": r"^(?:abstract|summary|r[eé]sum[eé]|resumo|samenvatting)\s*[:.]?$",
    "acknowledgements": r"^(?:acknowledg(?:e)?ments|agradecimentos|dankwoord)\s*[:.]?$",
    "references": r"^(?:references|bibliography|works cited|further reading)\s*[:.]?$",
}
_ROLE_PATTERNS = (
    (
        "translator",
        r"(?:translated(?:\s+from\s+the\s+\w+)?(?:\s+into\s+\w+)?\s+by|translation\s+by)\s+(.+)",
    ),
    (
        "foreword_contributor",
        r"(?:with\s+)?(?:a[n]?\s+)?(?:foreword|introduction|preface)\s+by\s+(.+)",
    ),
    ("series_editor", r"(?:a\s+)?series\s+edit(?:ed\s+by|ors?\s*:)\s+(.+)"),
    ("designer", r"(?:cover\s+)?design(?:ed\s+by|\s*:)\s+(.+)"),
    ("photographer", r"(?:cover\s+)?photograph(?:y|er)?(?:\s+by|\s*:)\s+(.+)"),
)


def _claim(author, role, sample, evidence, family, strength, work="selected work"):
    return {
        "author": author,
        "role": role,
        "work": work,
        "page": int(sample["page"]),
        "evidence": evidence,
        "family": family,
        "priority": strength,
        "source": "text_work_rights_credit"
        if role == "author" and "right" in family
        else "text_strict_credit_block",
    }


def _clean_names(value):
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value)
    value = re.sub(r"[*†‡§\d]+", "", value).strip(" :;")
    return _names(value)


def claims(samples, *, kind, work_scope):
    result, regions = [], []
    for sample in samples:
        text = str(sample.get("text") or "")
        lines = [" ".join(v.split()) for v in text.splitlines() if v.strip()]
        compact = " ".join(text.split())
        for index, line in enumerate(lines):
            for role, pattern in _SECTION_FAMILIES.items():
                if re.search(pattern, line, re.I):
                    regions.append(
                        {
                            "role": role,
                            "page": int(sample["page"]),
                            "line": index,
                            "evidence": line,
                        }
                    )
            if re.search(r"(?i)copyright|all rights reserved|ISBN|©", line):
                regions.append(
                    {
                        "role": "copyright",
                        "page": int(sample["page"]),
                        "line": index,
                        "evidence": line,
                    }
                )
            for role, pattern in _ROLE_PATTERNS:
                match = re.fullmatch(pattern, line, re.I)
                people = _clean_names(match[1]) if match else []
                if people:
                    result.append(
                        _claim(
                            ", ".join(people),
                            role,
                            sample,
                            line,
                            "explicit_role_label",
                            0,
                            work="nested or related contribution",
                        )
                    )
            advisor = re.fullmatch(
                r"(.{5,100}?),\s*(?:Advisor|Supervisor|Chair)", line, re.I
            )
            if advisor:
                people = _clean_names(advisor[1])
                if people:
                    result.append(
                        _claim(
                            ", ".join(people),
                            "advisor",
                            sample,
                            line,
                            "explicit_advisor_label",
                            0,
                            work="degree supervision",
                        )
                    )
            # To X on an otherwise sparse page is a dedication proposition,
            # not a universal exclusion of X in a separate author occurrence.
            if line.casefold() == "to" and len(lines) <= 5 and index + 1 < len(lines):
                people = _clean_names(lines[index + 1])
                if people:
                    result.append(
                        _claim(
                            ", ".join(people),
                            "dedicatee",
                            sample,
                            line + " / " + lines[index + 1],
                            "dedication_to",
                            0,
                            work="dedication",
                        )
                    )

        # An em-dash can attach a professional affiliation to a personal
        # byline. Record both roles; do not treat the institution as a person.
        # The role predicate is essential: a dash alone may join two authors.
        if kind in {"generic", "scholarly_article", "web_article"} and int(sample["page"]) <= 2:
            for index, line in enumerate(lines[:12]):
                professional = re.fullmatch(
                    r"(.{5,90}?)\s*[—–]\s*(.{3,100}?)\s+(Research Associate|Research Fellow|Professor|Lecturer|Researcher)\s*", line, re.I
                )
                if not professional or index == 0:
                    continue
                people = _names(professional[1])
                preceding = " ".join(lines[max(0, index - 4):index])
                if len(people) == 1 and len(_words(preceding)) >= 4:
                    result.append(_claim(
                        people[0], "author", sample, line,
                        "title_adjacent_professional_role", 105,
                        work="selected article",
                    ))
                    regions.append({"role": "affiliation", "page": int(sample["page"]), "line": index,
                                    "evidence": professional[2] + " " + professional[3]})

        if re.search(
            r"(?i)\b(?:ISBN|copyright|all rights reserved|copyright act)\b|©", compact
        ):
            for family, pattern in _RIGHTS_FAMILIES:
                for match in re.finditer(pattern, compact, re.I):
                    people = _names(match[1])
                    if not people and family == "person_asserts_moral_right":
                        tokens = match[1].split()
                        # Sentence boilerplate is outside the person's name.
                        # Preserve initials while trying bounded name suffixes.
                        for start in range(max(0, len(tokens) - 8), len(tokens) - 1):
                            people = _names(" ".join(tokens[start:]))
                            if people:
                                break
                    if people:
                        result.append(
                            _claim(
                                ", ".join(people),
                                "author",
                                sample,
                                match[0],
                                family,
                                110,
                                work="current book or parent work",
                            )
                        )
        # A labelled local contributor block precedes a numbered chapter's
        # prose. Read continuation until its paragraph boundary; never accept
        # a prefix whose final person is split across lines.
        if re.search(r"(?m)^\s*\d{1,3}[.]\s+\S", text):
            match = re.search(
                r"(?im)^\s*Contributors?\s*:\s*([^\n]+(?:\n(?!\s*\n)[^\n]+){0,7})", text
            )
            if match:
                raw = " ".join(match[1].split())
                people = _names(raw)
                if people and len(people) >= 2 and len(raw) <= 900:
                    result.append(
                        _claim(
                            ", ".join(people),
                            "author",
                            sample,
                            match[0],
                            "local_chapter_contributors",
                            115,
                            work="selected chapter",
                        )
                    )
        # A closing editorial roster has numbered affiliations and a separate
        # correspondence/ORCID apparatus. Thanks names alone do not qualify.
        if (
            kind in {"generic", "scholarly_article"}
            and re.search(r"(?i)\bORCID\b", text)
            and re.search(r"(?i)\bCorrespondence\b", text)
        ):
            stop = next(
                (
                    i
                    for i, line in enumerate(lines)
                    if re.match(
                        r"(?i)^\d?\s*(?:Department|School|University|Faculty|Industrial|Electrical)\b",
                        line,
                    )
                ),
                None,
            )
            if stop is not None:
                roster = []
                for line in reversed(lines[:stop]):
                    clean = re.sub(r"\d+$", "", line).strip()
                    people = _clean_names(clean)
                    if not people:
                        break
                    roster[0:0] = people
                if len(roster) >= 2:
                    result.append(
                        _claim(
                            ", ".join(roster),
                            "author",
                            sample,
                            " / ".join(lines[max(0, stop - len(roster)) : stop + 2]),
                            "closing_affiliated_roster",
                            103,
                        )
                    )
    # Recommended citation is a responsibility statement, while inquiry
    # contacts are merely access points. Require a matching research act.
    all_text = "\n".join(str(s.get("text") or "") for s in samples)
    if (
        kind in {"report", "generic", "corporate_publication"}
        and work_scope != "whole_issue"
    ):
        for sample in samples:
            match = re.search(
                r"(?im)^\s*(?:recommended|suggested) citation\s*:?\s*\n\s*([^,\n]{3,90})",
                str(sample.get("text") or ""),
            )
            if not match:
                continue
            organization = " ".join(match[1].split())
            organization_like = re.search(
                r"(?i)\b(?:center|centre|institute|university|agency|organization|organisation|foundation|council)\b",
                organization,
            )
            responsibility = re.search(
                re.escape(organization)
                + r"\s+(?:conducted|prepared|produced|published)\s+(?:this|the)\s+(?:study|report|research)",
                all_text,
                re.I,
            )
            if organization_like and responsibility:
                claim = _claim(
                    organization,
                    "author",
                    sample,
                    match[0] + " / " + responsibility[0],
                    "corporate_citation_and_responsibility",
                    104,
                )
                claim["source"] = "text_corporate_cover_byline"
                result.append(claim)
    # Interview responsibility needs both the titled conversation relation and
    # a questioner's labelled turn. A title's 'with' alone is insufficient.
    if work_scope != "whole_issue" and re.search(r"(?i)in[-\s]*conversation", all_text):
        for sample in samples:
            for match in re.finditer(
                r"(?m)^\s*([^:\n]{5,90}?)\s*\(([^)\n]{2,45})\):\s*([^\n]+)",
                str(sample.get("text") or ""),
            ):
                people = _names(match[1])
                if len(people) != 1 or re.search(
                    r"(?i)translator|interviewee|respondent", match[2]
                ):
                    continue
                linked = re.search(
                    r"\bwith\s+" + re.escape(people[0]) + r"\b", all_text, re.I
                )
                following = str(sample.get("text") or "")[
                    match.end() : match.end() + 300
                ]
                if linked and "?" in match[3] + following:
                    result.append(
                        _claim(
                            people[0],
                            "author",
                            sample,
                            match[0],
                            "interviewer_title_and_question_turn",
                            107,
                            work="selected interview",
                        )
                    )
    # A bare ownership line needs a second, separate title/creator occurrence.
    # This family never treats every person on a permissions page as author.
    if kind in {"book", "generic"} and work_scope not in {"chapter", "whole_issue"}:
        for sample in samples:
            for line in str(sample.get("text") or "").splitlines():
                match = re.fullmatch(
                    r"\s*(?:text\s+)?(?:copyright\s*:?[ ]*©?\s*|©\s*)(?:(?:18|19|20)\d{2}\s*(?:by\s+)?)?(.{5,180}?)\s*(?:(?:18|19|20)\d{2})?[., ]*",
                    line,
                    re.I,
                )
                people = _names(match[1]) if match else []
                # Publisher ownership can have the same name shape and repeat
                # on a title page. This weak corroboration family needs a
                # personal owner, unlike an explicit authorship assertion.
                if not people or re.search(
                    r"(?i)\b(?:publishers?|press|books|group|limited|ltd|incorporated|inc)\b",
                    match[1],
                ):
                    continue
                for other in samples:
                    if other["page"] == sample["page"] or int(other["page"]) > 10:
                        continue
                    raw = str(other.get("text") or "")
                    lines = [" ".join(v.split()) for v in raw.splitlines() if v.strip()]
                    if len(raw.split()) > 160 or re.search(
                        r"(?i)\b(?:series|praise|foreword|contents|translated by)\b",
                        raw,
                    ):
                        continue
                    credit = next(
                        (
                            v
                            for v in lines
                            if _words(v.removeprefix("By ").removeprefix("by "))
                            == _words(", ".join(people))
                        ),
                        None,
                    )
                    title_words = sum(len(_words(v)) for v in lines if v != credit)
                    if credit and title_words >= 3:
                        result.append(
                            _claim(
                                ", ".join(people),
                                "author",
                                sample,
                                line.strip() + " / title-page: " + credit,
                                "copyright_plus_separate_title_credit",
                                100,
                            )
                        )
                        break

    # Ownership does not equal authorship. A complete personal owner roster
    # can corroborate the SAME complete opening byline above an abstract on
    # a separate page, including working papers with long address apparatus.
    if kind in {"generic", "scholarly_article", "report"} and work_scope not in {"chapter", "whole_issue"}:
        for sample in samples:
            for line in str(sample.get("text") or "").splitlines():
                owner = re.fullmatch(
                    r"\s*(?:copyright\s*©?\s*|©\s*)(?:18|19|20)\d{2}\s*(?:by\s+)?(.{5,400}?)\s*[.]?\s*", line, re.I
                )
                if not owner or re.search(r"(?i)\b(?:press|publishers?|books|ltd|inc|group)\b", owner[1]):
                    continue
                people = _names(owner[1].rstrip('. '))
                if not people:
                    continue
                for other in samples:
                    if other["page"] == sample["page"] or int(other["page"]) > 3:
                        continue
                    opening = [" ".join(v.split()) for v in str(other.get("text") or "").splitlines() if v.strip()]
                    for index, credit in enumerate(opening[1:12], 1):
                        same = _names(credit)
                        if not same or _words(", ".join(same)) != _words(", ".join(people)):
                            continue
                        preceding = " ".join(opening[:index])
                        following = " ".join(opening[index+1:index+9])
                        if (3 <= len(_words(preceding)) <= 50
                                and re.search(r"(?i)\babstract\b", following)
                                and not re.search(r"(?i)\b(?:praise|foreword|translated|contents|references|bibliography)\b", preceding)):
                            result.append(_claim(
                                ", ".join(same), "author", other,
                                line.strip() + " / separate opening credit: " + credit,
                                "copyright_plus_opening_article_roster", 106,
                                work="selected article",
                            ))

    return result, regions


def select(current, assertions, *, kind, work_scope):
    """Specific responsibility can overturn an incumbent; region alone cannot."""
    if work_scope == "whole_issue":
        return (
            current,
            "whole issue has no single creator; contribution claims stay local",
        )
    eligible = []
    for claim in assertions:
        if claim["role"] != "author":
            continue
        if (
            claim["family"] == "corporate_citation_and_responsibility"
            and current.get("author")
            and _names(current["author"])
            and current.get("source")
            in {
                "text_byline",
                "text_written_by",
                "text_author_label",
                "text_report_prepared_by",
            }
        ):
            # An institution can conduct research and prescribe an institutional
            # citation while explicitly crediting personal report authors.
            continue
        if claim["family"] in {
            "title_adjacent_professional_role",
            "copyright_plus_opening_article_roster",
            "local_chapter_contributors",
            "closing_affiliated_roster",
            "corporate_citation_and_responsibility",
            "interviewer_title_and_question_turn",
            "local_chapter_with_two_later_author_heads",
        }:
            eligible.append(claim)
        elif kind in {"book", "generic"} and work_scope not in {
            "chapter",
            "whole_issue",
        }:
            eligible.append(claim)
    if not eligible:
        return current, "retain incumbent; no stronger scoped responsibility assertion"
    best_priority = max(c["priority"] for c in eligible)
    best = [c for c in eligible if c["priority"] == best_priority]
    keys = {
        tuple(sorted(_words(n) for n in _names(c["author"]))) or (_words(c["author"]),)
        for c in best
    }
    if len(keys) != 1:
        # Explicit conflicting responsibility is unresolved, never first-wins.
        return {
            "author": "",
            "source": "not_found",
        }, "defer conflicting same-work responsibility assertions"
    chosen = best[0]
    return {k: chosen[k] for k in ("author", "source", "page", "evidence")} | {
        "mechanism": chosen["family"]
    }, "select highest-priority scoped responsibility after collection"


def running_chapter_claims(bundle, *, kind, work_scope):
    """A local chapter byline plus two later author heads resolves OCR variants.

    This preserves the spelling actually read in the later evidence. It does
    not invent a missing name or globally repair a surname from a filename.
    """
    if work_scope == "whole_issue" or kind not in {"generic", "book_chapter"}:
        return []
    if (
        bundle.get("trace", {}).get("document_page_count", 0) > 80
        and work_scope != "chapter"
    ):
        return []
    headers, openings = [], []
    for region in bundle.get("ocr", []):
        lines = sorted(region["lines"], key=lambda v: (v["box"][1], v["box"][0]))
        for line in lines:
            if line["box"][1] < region["height"] * 0.13:
                raw = re.sub(r"^\d{1,4}\s+|\s+\d{1,4}$", "", line["text"]).strip()
                names = _names(raw)
                if len(names) == 1:
                    headers.append((region["page"], names[0], line["text"]))
        for i, line in enumerate(lines):
            if not re.fullmatch(r"CHAPTER\s*\d{1,3}", line["text"], re.I):
                continue
            title = []
            for candidate in lines[i + 1 : i + 15]:
                if candidate["box"][1] > region["height"] * 0.65:
                    break
                names = _names(candidate["text"])
                if len(names) == 1 and len(_words(" ".join(title))) >= 3:
                    openings.append(
                        (
                            region["page"],
                            names[0],
                            " / ".join([line["text"]] + title + [candidate["text"]]),
                        )
                    )
                title.append(candidate["text"])
    found = []
    for page, local, evidence in openings:
        local_key = _words(local)
        grouped = {}
        for later, name, raw in headers:
            key = _words(name)
            if (
                not page < later <= page + 3
                or len(key) != len(local_key)
                or key[:-1] != local_key[:-1]
            ):
                continue
            a, b = key[-1], local_key[-1]
            if a != b and (len(a) != len(b) or sum(x != y for x, y in zip(a, b)) > 1):
                continue
            grouped.setdefault(key, []).append((later, name, raw))
        for key, occurrences in grouped.items():
            pages = sorted({r[0] for r in occurrences})
            if len(pages) < 2:
                continue
            claim = _claim(
                occurrences[0][1],
                "author",
                {"page": page},
                evidence,
                "local_chapter_with_two_later_author_heads",
                116,
                work="selected chapter",
            )
            claim.update(
                local_ocr_spelling=local,
                corroboration_pages=pages,
                corroboration=[r[2] for r in occurrences],
                scope_reason="local chapter heading and byline followed by repeated full author heads",
            )
            found.append(claim)
    return found
