"""Title-page authorship for degree theses and dissertations."""

import re

from rag_pdf_tools import normalize_text

from .genre_evidence import credited_names, result
from .names import looks_like_person_name, normalize_author_candidate
from .profile import thesis_title_pages


_ROLE_START = re.compile(r"^(?:thesis\s+committee|committee|supervisors?|advisors?|examiners?)\s*:?$", re.I)
_AFFILIATION = re.compile(
    r"^(?:department|school|faculty|college|university|institute|division)\s+of\b|"
    r"^university\b|\bHonors College\s*$|^[A-Z]{2,8}(?:-[A-Z]{1,8})?-\d{2,4}-\d{2,6}$",
    re.I,
)
_ORGANIZATION = re.compile(
    r"^(?:department|school|faculty|college|university|institute|division|"
    r"thesis|dissertation|copyright|submitted)\b",
    re.I,
)


def infer(context):
    candidates = []
    for sample in context.opening_pages:
        text = str(sample.get("text") or "")
        if (re.search(r"\bA Thesis\b", text, re.I)
                and re.search(r"\bHonors College\b", text, re.I)
                and re.search(r"\b(?:Submitted|Presented)\s+to\b", text, re.I)):
            credit = re.search(r"(?im)^\s*by\s*\n\s*([^\n]{5,90})", text)
            if credit:
                name = normalize_author_candidate(credit.group(1))
                if looks_like_person_name(name):
                    return result(context, [name], "text_thesis_titlepage_author",
                                  int(sample["page"]), credit.group(0))
        if not re.search(r"\bHonors Research Projects\b", text, re.I):
            continue
        lines = [normalize_text(line) for line in text.splitlines() if normalize_text(line)]
        for index, line in enumerate(lines[:35]):
            if index + 1 >= len(lines) or "@" not in lines[index + 1]:
                continue
            if looks_like_person_name(line):
                return result(context, [line], "text_thesis_titlepage_author",
                              int(sample["page"]), line + " / " + lines[index + 1])
    for page, text in thesis_title_pages(context.opening_pages):
        lines = [normalize_text(line) for line in text.splitlines() if normalize_text(line)]
        for index, line in enumerate(lines[:45]):
            if not re.fullmatch(r"Dissertation Prepared for the Degree of", line, re.I) or index == 0:
                continue
            name = re.sub(r"(?:,?\s*(?:B\.?A\.?|M\.?A\.?|M\.?S\.?|Ph\.?D\.?))*\s*$", "", lines[index - 1], flags=re.I).strip(" ,")
            if looks_like_person_name(name):
                return result(context, [name], "text_thesis_titlepage_author", page,
                              lines[index - 1] + " / " + line)
        for index, line in enumerate(lines[:32]):
            if _ROLE_START.match(line):
                break
            following = lines[index + 1] if index + 1 < len(lines) else ""
            byline = re.fullmatch(r"by\s+(.+)", line, flags=re.I)
            if byline:
                names = credited_names(byline.group(1))
                if len(names) == 1:
                    candidates.append((page, names[0], line))
                continue
            if line.casefold() == "by" and following:
                names = credited_names(following)
                if not names and re.search(r"\b[A-Z]\s+[a-z]{2,}\b", following):
                    repaired = re.sub(r"\b([A-Z])\s+([a-z]{2,})\b", r"\1\2", following)
                    names = credited_names(repaired)
                if len(names) == 1:
                    candidates.append((page, names[0], f"By / {following}"))
                continue
            if re.fullmatch(r"A\s+Dissertation", following, re.I):
                names = credited_names(line)
                if len(names) == 1:
                    candidates.append((page, names[0], f"{line} / {following}"))
                continue
            if (_ORGANIZATION.match(line)
                    or re.search(r"\b(?:Committee|Dean|Advisor|Supervisor|Examiner)\b", line, re.I)
                    or not _AFFILIATION.match(following)):
                continue
            name = normalize_author_candidate(line)
            if looks_like_person_name(name, title_hint="", allow_all_caps=True):
                candidates.append((page, name, f"{line} / {following}"))
    distinct = {(page, name.casefold()): (page, name, evidence)
                for page, name, evidence in candidates}
    if len(distinct) == 1:
        page, name, evidence = next(iter(distinct.values()))
        return result(context, [name], "text_thesis_titlepage_author", page, evidence)
    return {"author": "", "source": "thesis_author_not_resolved", "page": 0,
            "evidence": "conflicting title-page people" if distinct else ""}
