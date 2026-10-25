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
        r"\bmoral\s+rights?\s+of\s+(.{5,180}?)\s+as\s+(?:the\s+)?authors?\s+(?:(?:has|have)\s+been|are|is)\s+asserted",
    ),
    (
        "attribution_as_author_right",
        r"\b(?:the\s+)?rights?\s+of\s+(.{5,180}?)\s+to\s+be\s+(?:identified|named|credited)\s+as\s+(?:the\s+)?authors?\b",
    ),
    (
        "authorship_attributed_to",
        r"\bauthorship\s+of\s+(?:this|the\s+present)\s+(?:work|book|volume|edition)\s+is\s+attributed\s+to\s+(.{5,160}?)(?=\s+(?:ISBN|Copyright|All rights reserved)\b|[;©]|$)",
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


def claims(samples, *, kind, work_scope, opening_pages=None):
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
            # The actor, author-role and rights act can occur in either order.
            # These short apparatus clauses do not rely on a particular legal
            # boilerplate sentence or on an ownership symbol being present.
            if (
                int(sample["page"]) in opening_pages
                if opening_pages is not None
                else 0 <= int(sample["page"]) <= 12
            ) and len(line) <= 320:
                predicates = (
                    r"^(.{5,150}?)\s+(?:has\s+|have\s+)?(?:asserted|asserts|claims)\s+(?:(?:his|her|their|the)\s+)?(?:moral\s+)?rights?\s+(?:to\s+(?:be\s+named|identification|attribution)\s+as\s+authors?|as\s+authors?)\b",
                    r"^(.{5,150}?)\s+(?:asserts|claims)\s+authorship\s+of\s+(?:this|the\s+present)\s+(?:work|book|volume|edition)\b",
                    r"^(.{5,150}?)\s+is\s+identified\s+as\s+(?:the\s+)?author\s+of\s+this\s+(?:work|book|volume|edition)\b",
                    r"^The\s+authors?\s*,\s*(.{5,150}?)\s*,\s*retains?\s+attribution\s+rights?\b",
                )
                for pattern in predicates:
                    assertion = re.search(pattern, line, re.I)
                    people = _clean_names(assertion[1]) if assertion else []
                    if people:
                        result.append(
                            _claim(
                                ", ".join(people),
                                "author",
                                sample,
                                line,
                                "author_role_and_attribution_right",
                                110,
                                work="current book or parent work",
                            )
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
        if (
            kind in {"generic", "scholarly_article", "web_article"}
            and int(sample["page"]) <= 2
        ):
            for index, line in enumerate(lines[:12]):
                professional = re.fullmatch(
                    r"(.{5,90}?)\s*[—–]\s*(.{3,100}?)\s+(Research Associate|Research Fellow|Professor|Lecturer|Researcher)\s*",
                    line,
                    re.I,
                )
                if not professional or index == 0:
                    continue
                people = _names(professional[1])
                preceding = " ".join(lines[max(0, index - 4) : index])
                if len(people) == 1 and len(_words(preceding)) >= 4:
                    result.append(
                        _claim(
                            people[0],
                            "author",
                            sample,
                            line,
                            "title_adjacent_professional_role",
                            105,
                            work="selected article",
                        )
                    )
                    regions.append(
                        {
                            "role": "affiliation",
                            "page": int(sample["page"]),
                            "line": index,
                            "evidence": professional[2] + " " + professional[3],
                        }
                    )

        if re.search(
            r"(?i)\b(?:ISBN|copyright|all rights reserved|copyright act|moral rights?|authorship|attribution rights?)\b|©",
            compact,
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
        if work_scope != "whole_issue" and re.search(r"(?m)^\s*\d{1,3}[.]\s+\S", text):
            roster = re.search(
                r"(?im)^\s*Lead authors?\s*:\s*([^\n]+)\n"
                r"\s*Contributing authors?\s*:\s*([^\n]+(?:\n(?!\s*\n)[^\n]+){0,7})",
                text,
            )
            if roster:
                leads = _clean_names(" ".join(roster[1].split()))
                contributors = _clean_names(" ".join(roster[2].split()))
                if leads and contributors:
                    result.append(
                        _claim(
                            ", ".join(dict.fromkeys(leads + contributors)),
                            "author",
                            sample,
                            roster[0],
                            "local_chapter_contributors",
                            115,
                            work="selected chapter",
                        )
                    )
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
    if kind in {"generic", "scholarly_article", "report"} and work_scope not in {
        "chapter",
        "whole_issue",
    }:
        for sample in samples:
            for line in str(sample.get("text") or "").splitlines():
                owner = re.fullmatch(
                    r"\s*(?:copyright\s*©?\s*|©\s*)(?:18|19|20)\d{2}\s*(?:by\s+)?(.{5,400}?)\s*[.]?\s*",
                    line,
                    re.I,
                )
                if not owner or re.search(
                    r"(?i)\b(?:press|publishers?|books|ltd|inc|group)\b", owner[1]
                ):
                    continue
                people = _names(owner[1].rstrip(". "))
                if not people:
                    continue
                for other in samples:
                    if other["page"] == sample["page"] or int(other["page"]) > 3:
                        continue
                    opening = [
                        " ".join(v.split())
                        for v in str(other.get("text") or "").splitlines()
                        if v.strip()
                    ]
                    for index, credit in enumerate(opening[1:12], 1):
                        same = _names(credit)
                        if not same or _words(", ".join(same)) != _words(
                            ", ".join(people)
                        ):
                            continue
                        preceding = " ".join(opening[:index])
                        following = " ".join(opening[index + 1 : index + 9])
                        if (
                            3 <= len(_words(preceding)) <= 50
                            and re.search(r"(?i)\babstract\b", following)
                            and not re.search(
                                r"(?i)\b(?:praise|foreword|translated|contents|references|bibliography)\b",
                                preceding,
                            )
                        ):
                            result.append(
                                _claim(
                                    ", ".join(same),
                                    "author",
                                    other,
                                    line.strip()
                                    + " / separate opening credit: "
                                    + credit,
                                    "copyright_plus_opening_article_roster",
                                    106,
                                    work="selected article",
                                )
                            )

    return result, regions


def title_copyright_claims(proposals, samples, *, opening_pages=None):
    """A title-bound roster and matching ownership are stronger together."""
    result = []
    for proposal in proposals:
        if proposal.get("expert") not in {
            "structured_credit",
            "publisher_titlepage",
            "portable_book",
        }:
            continue
        names = _names(proposal.get("author", ""))
        page = int(proposal.get("page") or 0)
        if not names or (
            page not in opening_pages
            if opening_pages is not None
            else not 1 <= page <= 12
        ):
            continue
        for sample in samples:
            if int(sample.get("page") or 0) != page:
                continue
            for match in re.finditer(
                r"(?im)^\s*(?:Copyright\s*©?|©)\s*(?:\d{4}\s*)?(?:by\s+)?([^\n]+)",
                sample["text"],
            ):
                owner = _clean_names(match[1].strip().rstrip("."))
                if sorted(_words(n) for n in owner) != sorted(_words(n) for n in names):
                    continue
                result.append(
                    _claim(
                        ", ".join(names),
                        "author",
                        sample,
                        proposal.get("evidence", "") + " / " + match[0].strip(),
                        "title_credit_and_matching_personal_copyright",
                        108,
                        work="selected work",
                    )
                )
    return result


def local_credit_role_claims(proposals, assertions):
    """Explain a wrapper roster using a narrower physical credit and roles."""
    translators = {
        "".join(_words(name))
        for claim in assertions
        if claim.get("role") in {"translator", "translated", "translation"}
        for name in _names(claim.get("author", ""))
    }
    if not translators:
        return []
    result = []
    wrappers = [
        c
        for c in assertions
        if c.get("family") == "labelled_responsibility_field"
        and c.get("role") == "author"
        and c.get("work") == "selected work"
    ]
    for wrapper in wrappers:
        roster = _names(wrapper["author"])
        kept = [n for n in roster if "".join(_words(n)) not in translators]
        if not kept or len(kept) == len(roster):
            continue
        local_proposals = list(proposals) + [
            {**c, "expert": "local_explicit_byline"}
            for c in assertions
            if c.get("family") == "local_explicit_byline"
        ]
        for proposal in local_proposals:
            evidence = proposal.get("evidence", "")
            if (
                proposal.get("expert")
                not in {"structured_credit", "local_explicit_byline"}
                or not re.search(r"(?:^|/|\n)\s*by\s+", evidence, re.I)
                or int(proposal.get("page") or 0) > 5
                or sorted(
                    "".join(_words(n)) for n in _names(proposal.get("author", ""))
                )
                != sorted("".join(_words(n)) for n in kept)
            ):
                continue
            result.append(
                _claim(
                    ", ".join(kept),
                    "author",
                    {"page": proposal["page"]},
                    evidence + " / wrapper roster: " + wrapper["author"],
                    "local_byline_with_separate_translation_credit",
                    112,
                    work="selected work",
                )
            )
    return result


def primary_creator_role_claims(proposals, *, kind, work_scope):
    """Use a specialist's explicit primary/secondary title-page relation."""
    if kind not in {"book", "generic"} or work_scope in {"chapter", "whole_issue"}:
        return []
    result = []
    for proposal in proposals:
        boundary = proposal.get("secondary_role_boundary", {})
        if (
            proposal.get("mechanism") != "compact_publisher_titlepage_role_order"
            or not re.search(
                r"\b(?:edited by|translated by)\b", boundary.get("text", ""), re.I
            )
            or not _names(proposal.get("author", ""))
        ):
            continue
        result.append(
            {
                **proposal,
                "role": "author",
                "work": "selected work",
                "family": "primary_creators_before_edition_role",
                "priority": 108,
                "basis": "The physical title-page expert separates the primary creator block from a later edition-editor or translation credit",
            }
        )
    return result


def visible_credit_truncation_claims(assertions, bundle):
    """Visible complete role credits can explain an OCR-truncated roster.

    Keep both readings. This contextual priority requires the same page, role,
    full preceding people and an otherwise identical final surname prefix.
    Native text has no blanket priority over OCR or a genuine different roster.
    """
    pages = {p["page"]: p for p in (bundle or {}).get("native", [])}
    result = []
    for claim in assertions:
        if (
            claim.get("family") != "explicit_publishing_act_span"
            or claim.get("role") not in {"author", "editor"}
            or "ocr" in claim.get("extraction", "")
            or claim.get("work") != "selected work"
        ):
            continue
        names = _names(claim.get("author", ""))
        if len(names) < 2:
            continue
        visible = [
            line["text"]
            for line in pages.get(claim.get("page"), {}).get("horizontal_lines", [])
            if line.get("visible")
        ]
        if not any(
            re.search(r"\b(?:edited by|editors?|written by|authors?|by)\b", line, re.I)
            for line in visible
        ):
            continue
        words = _words(" ".join(visible))
        roster = [_words(name) for name in names]
        if not all(
            any(
                words[i : i + len(name)] == name
                for i in range(len(words) - len(name) + 1)
            )
            for name in roster
        ):
            continue
        for other in assertions:
            if (
                "ocr" not in other.get("extraction", "")
                or other.get("page") != claim.get("page")
                or other.get("role") != claim.get("role")
                or other.get("work") != claim.get("work")
            ):
                continue
            partial = [_words(name) for name in _names(other.get("author", ""))]
            if len(partial) != len(roster) or partial[:-1] != roster[:-1]:
                continue
            short, full = partial[-1], roster[-1]
            if (
                len(short) != len(full)
                or short[:-1] != full[:-1]
                or not 3 <= len(short[-1]) <= len(full[-1]) - 2
                or not full[-1].startswith(short[-1])
            ):
                continue
            result.append(
                {
                    **claim,
                    "family": "visible_complete_credit_ocr_truncation",
                    "priority": 107,
                    "basis": "Visible role and every complete name explain a same-page OCR truncation; a different complete surname would remain a conflict",
                    "truncated_reading": other["author"],
                }
            )
            break
    return result


def reconcile_report_foreword(current, bundle, proposals, assertions):
    """A local foreword byline does not alone identify a larger report's author."""
    if (current.get("source") != "text_structured_opening_credit"
            or current.get("mechanism") != "physical_heading_byline_body"
            or int(current.get("page") or 0) <= 1
            or not re.match(r"\s*foreword\b", current.get("evidence", ""), re.I)):
        return current, None
    native = (bundle or {}).get("native", [])
    page = int(current["page"])
    cover = next((p.get("text", "") for p in native if p.get("page") == 1), "")
    if (not re.search(r"\breport\b", cover[:700], re.I)
            or re.search(r"\bforeword\b", cover[:700], re.I)
            or (bundle or {}).get("trace", {}).get("document_page_count", 0) <= page + 2):
        return current, None
    contents = [p for p in native if 1 < int(p.get("page") or 0) < page
                and re.search(r"\bcontents\b", p.get("text", "")[:600], re.I)
                and re.search(r"\bforeword\b", p.get("text", ""), re.I)
                and re.search(r"\b(?:introduction|chapter)\b", p.get("text", ""), re.I)]
    if not contents:
        return current, None
    # Independent work-level responsibility can establish that a report author
    # also wrote its foreword. Repeated copies of this local byline cannot.
    strong = {"text_author_label", "text_byline", "text_work_rights_credit",
              "text_bibliographic_author_label", "text_corporate_cover_byline"}
    actor = _words(current.get("author", ""))
    if any(_words(p.get("author", "")) == actor and p.get("source") in strong
           for p in [*proposals, *assertions]):
        return current, None
    audit = {"author": current["author"], "role": "foreword contributor",
             "work": "nested foreword", "page": page,
             "contents_pages": [p["page"] for p in contents],
             "basis": "A separate report cover and contents place this geometric byline in a foreword before the main chapters; no independent whole-report credit supports the person."}
    return {"author": "", "source": "not_found", "page": 0,
            "evidence": audit["basis"]}, audit


def select(current, assertions, *, kind, work_scope):
    """Specific responsibility can overturn an incumbent; region alone cannot."""
    if work_scope == "whole_issue":
        return (
            current,
            "whole issue has no single creator; contribution claims stay local",
        )
    eligible = []
    for claim in assertions:
        if claim.get("source") == "text_role_scoped_creator":
            if claim.get(
                "family"
            ) == "personal_blog_masthead_current_post" and current.get("author"):
                # Site ownership and first-person voice are contextual support,
                # not authority to erase an already accepted post creator.
                continue
            if claim.get("work") == "selected work":
                explicit_personal = current.get("source") in {
                    "text_byline",
                    "text_written_by",
                    "text_author_label",
                    "text_report_prepared_by",
                } or current.get("mechanism") in {
                    "complete_native_author_band",
                    "title_author_affiliation_pairs_before_abstract",
                    "title_author_affiliation_pairs_before_body",
                    "complete_title_bound_email_roster",
                    "feature_deck_then_author_byline",
                }
                if claim.get("creator_type") == "organization" and explicit_personal:
                    # Issuing/research responsibility can coexist with named
                    # personal writers. Its corporate role does not erase an
                    # explicit title-bound complete personal byline.
                    continue
                eligible.append(claim)
            continue
        span_family = claim["family"] in {
            "labelled_responsibility_field",
            "explicit_publishing_act_span",
            "visible_complete_credit_ocr_truncation",
        }
        if span_family:
            if claim.get("work") != "selected work" or claim.get("priority", 0) <= 0:
                continue
            if claim["role"] == "author" or (
                claim["role"] == "editor"
                and kind in {"book", "generic"}
                and work_scope not in {"chapter", "whole_issue"}
            ):
                eligible.append(claim)
            continue
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
            "local_byline_with_separate_translation_credit",
            "local_explicit_byline",
            "title_credit_and_matching_personal_copyright",
            "physical_complete_email_roster",
            "physical_complete_mixed_roster",
            "physical_complete_native_roster",
            "physical_opening_byline_before_body",
            "primary_creators_before_edition_role",
            "local_chapter_credit_with_repeated_running_author",
            "visible_complete_credit_ocr_truncation",
            "embedded_creator_with_visible_responsibility",
            "embedded_bibliography_with_printed_parent_credit",
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
            "source": "conflicting_work_credits",
            "page": 0,
            "evidence": " / ".join(
                dict.fromkeys(
                    f"page {claim.get('page', 0)}: {claim['author']}" for claim in best
                )
            ),
        }, "defer conflicting same-work responsibility assertions"
    chosen = best[0]
    if current.get("author") and _words(current["author"]) == _words(chosen["author"]):
        return (
            current,
            "scoped responsibility corroborates incumbent; retain provenance",
        )
    return {k: chosen[k] for k in ("author", "source", "page", "evidence")} | {
        "mechanism": chosen["family"],
        **(
            {"creator_type": chosen["creator_type"]} if "creator_type" in chosen else {}
        ),
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


def opening_byline_claims(proposals, bundle, *, kind, work_scope):
    """Bind a geometric opening credit to the abstract of the selected article."""
    if kind != "scholarly_article" or work_scope == "whole_issue":
        return []
    page = next(
        (p for p in (bundle or {}).get("native", []) if p.get("page") == 1), None
    )
    if page is None:
        return []
    result = []
    for proposal in proposals:
        if (
            proposal.get("page") != 1
            or proposal.get("mechanism") != "physical_heading_byline_body"
        ):
            continue
        box = proposal.get("geometry", {}).get("byline")
        if (
            not isinstance(box, (list, tuple))
            or len(box) != 4
            or not all(isinstance(v, (int, float)) for v in box)
        ):
            continue
        body = [
            line
            for line in page.get("horizontal_lines", [])
            if line["box"][1] >= box[3]
            and re.match(r"^\s*(?:abstract|summary)\b[:.]?", line["text"], re.I)
        ]
        if not body or not _names(proposal.get("author", "")):
            continue
        result.append(
            {
                "author": proposal["author"],
                "role": "author",
                "work": "selected work",
                "page": 1,
                "evidence": proposal["evidence"],
                "family": "physical_opening_byline_before_body",
                "priority": 107,
                "source": "text_scoped_responsibility_span",
                "basis": "Complete physical title/byline/body proposal precedes this article's abstract; later section headings cannot silently outrank it",
            }
        )
    return result
