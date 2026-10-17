"""Responsibility acts for organizations and non-Latin/local legal signatures.

These are typed publication credits, not a list of known names or publishers.
An issuer, recipient, opinion writer and nested contributor remain distinct.
"""

import re

from .structured_credit import _names, _words

_SOURCE = "text_role_scoped_creator"
_ORGANIZATION = re.compile(
    r"\b(?:office|agency|commission|council|department|institute|foundation|"
    r"organization|organisation|corporation|university|research|fund)\b",
    re.I,
)
_MONTH = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"


def _apparatus_claims(sample, lines, opening_text):
    """Dated web credits, current-work citation and instructional signatures."""
    result = []
    text = str(sample.get("text") or "")
    page = int(sample.get("page") or 0)
    if page == 1:
        for index, value in enumerate(lines[:25]):
            dated = re.match(
                r"^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*\s+\d{1,2}\s+"
                + _MONTH
                + r"\s+\d{4}\b",
                value,
                re.I,
            )
            if dated and index:
                candidate = lines[index - 1]
                names = _names(candidate)
                # A news agency is a valid byline actor. Its agency role is
                # explicit in the byline, independent of the hosting paper.
                agency = re.fullmatch(
                    r"[A-Z][\w&.'’ -]{2,70}\s+(?:Press|News Agency|News Service|News Bureau)",
                    candidate,
                )
                heading = " ".join(lines[: index - 1])
                if 4 <= len(_words(heading)) <= 110 and (names or agency):
                    result.append(
                        _claim(
                            ", ".join(names) if names else candidate,
                            sample,
                            candidate + " / " + value,
                            "dated_post_byline",
                            "person" if names else "organization",
                        )
                    )
            # The date and organizational byline can share a line. Require
            # the organization to identify itself in the actual post as well.
            dated_org = re.match(
                r"^" + _MONTH + r"\s+\d{1,2},?\s+\d{4}\s+([A-Z][\w&.'’ -]{1,60})$",
                value,
                re.I,
            )
            if dated_org:
                candidate = dated_org[1].strip()
                self_credit = re.search(
                    r"\b(?:At|We at)\s+(" + re.escape(candidate) + r")\s*,?\s+we\b",
                    text,
                    re.I,
                )
                if self_credit:
                    result.append(
                        _claim(
                            self_credit[1],
                            sample,
                            value + " / " + self_credit[0],
                            "dated_organizational_byline_and_self_statement",
                            "organization",
                        )
                    )
            # Blog review layouts can put the author before the title and
            # repeat the same person in a "writes Name" introduction.
            dated_person = re.fullmatch(
                r"(.{5,90}?)\s+" + _MONTH + r"\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}",
                value,
                re.I,
            )
            people = _names(dated_person[1]) if dated_person else []
            if len(people) == 1 and re.search(
                r"\bwrites\s+" + re.escape(people[0]) + r"[.!]", text[:2200], re.I
            ):
                result.append(
                    _claim(
                        people[0],
                        sample,
                        value + " / matching writes attribution",
                        "dated_byline_and_matching_writer_attribution",
                        "person",
                    )
                )
        if (
            re.search(
                r"(?im)^\s*(?:How to\b|.*\b(?:writing guide|instructions|worksheet|handout)\b)",
                text[:400],
            )
            and text.count("?") >= 3
        ):
            for value in lines[-3:]:
                signature = re.fullmatch(
                    r"(.{4,80}?)\s*\d{1,2}/\d{1,2}(?:/\d{2,4})?", value
                )
                people = _names(signature[1]) if signature else []
                if len(people) == 1:
                    result.append(
                        _claim(
                            people[0],
                            sample,
                            value,
                            "instructional_handout_dated_signature",
                            "person",
                        )
                    )
    for citation in re.finditer(
        r"(?i)\b(?:Suggested|Recommended) citation\s*:\s*([^\n,‘'“\"]{2,120}),?\s*[‘'“\"]([^’'”\"]{8,300})",
        " ".join(text.split()),
    ):
        actor, title = citation[1].strip(), citation[2]
        title_words = _words(title)
        source_words = _words(opening_text[:1500])
        title_matches = len(title_words) >= 3 and any(
            source_words[i : i + 3] == title_words[:3]
            for i in range(max(0, len(source_words) - 2))
        )
        if _ORGANIZATION.search(actor) and title_matches:
            result.append(
                _claim(
                    actor,
                    sample,
                    citation[0],
                    "current_work_suggested_institutional_citation",
                    "organization",
                )
            )
    return result


def _claim(author, sample, evidence, family, creator_type, priority=109):
    return {
        "author": author,
        "role": "author",
        "work": "selected work",
        "page": sample.get("page", 0),
        "evidence": evidence,
        "family": family,
        "source": _SOURCE,
        "priority": priority,
        "creator_type": creator_type,
    }


def claims(samples, *, kind, work_scope, opening_pages=None):
    result = []
    if work_scope == "whole_issue":
        return result
    opening = [
        s
        for s in samples
        if (
            int(s.get("page") or 0) in opening_pages
            if opening_pages is not None
            else 0 <= int(s.get("page") or 0) <= 12
        )
    ]
    all_text = "\n".join(str(s.get("text") or "") for s in samples)
    opening_text = next(
        (
            str(s.get("text") or "")
            for s in samples
            if int(s.get("page") or 0) in {0, 1}
        ),
        "",
    )
    for sample in opening:
        text = str(sample.get("text") or "")
        page = int(sample.get("page") or 0)
        lines = [" ".join(v.split()) for v in text.splitlines() if v.strip()]
        result.extend(_apparatus_claims(sample, lines, opening_text))
        if (
            re.search(r"(?im)^\s*Experts on this topic\s*$", text)
            and re.search(
                r"\bwe\s+are\s+(?:two|three|four|five|six|\d+)\s+authors?\b",
                all_text,
                re.I,
            )
            and re.search(r"\bin\s+this\s+report\s*,?\s+we\b", all_text, re.I)
        ):
            # Multi-column biographies can name the people collectively
            # responsible for a report. Professional roles and contact blocks
            # support each roster member; a biography about someone alone
            # never establishes authorship of the surrounding work.
            left = [
                re.split(r"\s{3,}", line.strip())[0]
                for line in text.splitlines()
                if line.strip()
            ]
            roster = []
            for index, value in enumerate(left[:-1]):
                people = _names(value)
                following = " ".join(left[index + 1 : index + 6])
                if (
                    len(people) == 1
                    and re.search(
                        r"\b(?:Engineer|Fellow|Professor|Researcher|Director|Scientist|Lecturer)\b",
                        left[index + 1],
                        re.I,
                    )
                    and re.search(r"@|linkedin|https?://", following, re.I)
                ):
                    roster.extend(people)
            if len(roster) >= 2:
                result.append(
                    _claim(
                        ", ".join(dict.fromkeys(roster)),
                        sample,
                        "Experts on this topic / affiliated contact biographies / we are authors / in this report we",
                        "biographic_roster_and_current_report_voice",
                        "person",
                    )
                )
        if page == 1 and re.search(r"\bDEPARTMENTAL\s+WRITING\s+GUIDE\b", text, re.I):
            university = next(
                (
                    line
                    for line in lines[:3]
                    if re.match(r"^(?:University of .+|.+ University)$", line)
                ),
                "",
            )
            units = [
                line
                for line in lines[:12]
                if line.isupper()
                and 2 <= len(_words(line)) <= 6
                and not re.search(r"guide|departmental|writing|updated|\d", line, re.I)
            ]
            if (
                university
                and len(units) == 1
                and re.search(r"\binstructors?\b|\bcourses?\b", all_text, re.I)
            ):
                result.append(
                    _claim(
                        university + ", " + units[0],
                        sample,
                        university + " / departmental guide / " + units[0],
                        "departmental_instructional_issuer",
                        "organization",
                    )
                )
        # Filing form labels explicitly distinguish the responsible registrant
        # from the regulator printed first on the cover.
        if re.search(r"\bFORM\s+10[-–]K\b", text, re.I):
            for index, line in enumerate(lines):
                if index and re.search(
                    r"Exact name of registrant as specified", line, re.I
                ):
                    candidate = lines[index - 1].strip("_ ")
                    if 2 <= len(candidate) <= 120 and not re.search(
                        r"file number|^form\b", candidate, re.I
                    ):
                        result.append(
                            _claim(
                                candidate,
                                sample,
                                candidate + " / " + line,
                                "filing_registrant",
                                "organization",
                            )
                        )
        if kind in {"corporate_publication", "report", "generic"}:
            # A personal title-page By credit remains the complete work credit;
            # the broader issuing institution does not erase it.
            personal_cover = any(
                _names(m[1])
                for m in re.finditer(r"(?im)^\s*by\s+([^\n]+)", text[:2500])
            )
            if not personal_cover and re.search(
                r"\b(?:revenue|financial results|earnings)\b", text, re.I
            ):
                headline = re.search(
                    r"(?m)^\s*([A-Z][\w&.,'’ -]{1,65}?)\s+Reports?\s+(?:Record\s+)?(?:Q[1-4]|First|Second|Third|Fourth|Fiscal|Full Year|Financial)\b",
                    text[:2500],
                )
                if headline:
                    issuer = headline[1].strip()
                    reporting = re.search(
                        re.escape(issuer)
                        + r"\s*\((?:NASDAQ|NYSE)\s*:[^)]+\)\s+today\s+(?:reported|announced)\b",
                        text,
                        re.I,
                    )
                    if reporting and re.search(
                        r"FOR IMMEDIATE RELEASE|Earnings Release", text, re.I
                    ):
                        result.append(
                            _claim(
                                issuer,
                                sample,
                                headline[0] + " / " + reporting[0],
                                "financial_release_reporting_agent",
                                "organization",
                            )
                        )
            # An organization actually performs this report's research and is
            # also named in its apparatus. Contact or sponsorship alone fails.
            if not personal_cover:
                for act in re.finditer(
                    r"(?m)(?:^|[.!?]\s+|[,\n]\s*)([A-Z][A-Za-z&.'’ -]{1,70}?)\s+(?:conducted|prepared|produced|surveyed)\s+([^.!?\n]{5,200})",
                    " ".join(text.split()),
                ):
                    issuer = " ".join(act[1].split())
                    if not _ORGANIZATION.search(issuer):
                        continue
                    target = act[2]
                    if not (
                        re.search(
                            r"\b(?:this|the)\s+(?:study|report|research|survey)|professionals|respondents|participants",
                            target,
                            re.I,
                        )
                        and len(re.findall(re.escape(issuer), all_text, re.I)) >= 2
                        and re.search(r"\breport|research|survey\b", all_text, re.I)
                    ):
                        continue
                    result.append(
                        _claim(
                            issuer,
                            sample,
                            act[0],
                            "organization_research_act_and_apparatus",
                            "organization",
                        )
                    )
            # Institutional audit/report letter: issuer before an addressed
            # recipient, repeated in the publication and speaking as auditor.
            if (
                int(sample.get("page") or 0) == 1
                and re.search(r"\bReport to\b", text, re.I)
                and re.search(r"\baudit\b", all_text, re.I)
            ):
                for line in lines[:6]:
                    if (
                        re.search(
                            r"\b(?:Office|Agency|Commission|Council)\s*$", line, re.I
                        )
                        and not re.search(
                            r"\b(?:report to|for the|department of)\b", line, re.I
                        )
                        and _words(all_text).count(_words(line)[-1]) >= 2
                    ):
                        result.append(
                            _claim(
                                line,
                                sample,
                                line + " / report to recipient / audit narrative",
                                "institutional_audit_issuer",
                                "organization",
                            )
                        )
        # Japanese review apparatus: book-author header is distinct from the
        # reviewer's parenthesized affiliation and signature before references.
        if re.search(r"書評|新刊紹介", text) and "ISBN" in text:
            for signature in re.finditer(
                r"[（(]([^（）()\n]{4,100}?)[\s　]+([\u3400-\u9fff]{2,8})[）)]", text
            ):
                if re.search(r"大学|研究|センター|図書館", signature[1]):
                    result.append(
                        _claim(
                            signature[2],
                            sample,
                            signature[0],
                            "affiliated_cjk_review_signature",
                            "person_cjk",
                        )
                    )
        if kind == "legal_opinion" or re.search(r"\bOpinion of the Court\b", text):
            for opinion in re.finditer(
                r"\b(?:JUSTICE\s+([A-Z][A-Za-z'’-]+)|([A-Z][A-Za-z'’-]+),\s*J\.,)\s+delivered\s+the\s+opinion(?:\s+of\s+the\s+Court)?",
                text,
                re.I,
            ):
                surname = opinion[1] or opinion[2]
                result.append(
                    _claim(
                        surname,
                        sample,
                        opinion[0],
                        "opinion_delivered_by_justice",
                        "person_surname",
                    )
                )
    return result
