"""Let visible work/role experts assess embedded creator proposals."""

import re
import unicodedata

from .identity import normalize_metadata_author
from .names import looks_like_person_name
from .structured_credit import _names


def _words(value):
    value = unicodedata.normalize("NFKD", str(value).casefold())
    return tuple(re.findall(r"[^\W_]+", "".join(
        c for c in value if not unicodedata.combining(c))))


def _contains(text, phrase):
    text, phrase = _words(text), _words(phrase)
    return bool(phrase) and any(text[i:i + len(phrase)] == phrase
                               for i in range(len(text) - len(phrase) + 1))


def _roster(raw):
    raw = re.sub(r",?\s*\b(?:1[5-9]\d{2}|20\d{2})\s*[-–]?\s*$", "", raw)
    return normalize_metadata_author(raw)


def _bibliography(raw):
    """A structured ebook export identifies a parent work, not its chapter."""
    match = re.fullmatch(r"([^,\n]{2,60}),,(.{2,80}?)\.,(.+)", raw.strip(), re.S)
    if not match or not _contains(raw, "ProQuest Ebook Central"):
        return None
    surname, given, tail = match.groups()
    year = re.search(r",,(?:18|19|20)\d{2}\.,ProQuest,Ebook,Central", tail)
    if not year or ",," not in tail[:year.start()]:
        return None
    title, publisher = tail[:year.start()].rsplit(",,", 1)
    credit = surname + ", " + given.replace(",", " ")
    if re.search(r"\b(?:ed|eds|edited|editor|editors|et\s+al)\b", credit, re.I):
        return None
    title = title.replace(",", " ")
    if re.search(r"\b(?:anthology|edited|editors?)\b", title, re.I):
        return None
    author = _roster(credit)
    if not looks_like_person_name(author) or len(_words(title)) < 3:
        return None
    return {"author": author, "title": title, "raw_credit": credit,
            "publisher": publisher.replace(",", " ")}


def assess(context, views):
    """Return proposals, expert assessments, and separately supported claims.

    Embedded credits alone never create an eligible responsibility claim. All
    native/OCR views have already been acquired; none is skipped after a match.
    """
    metadata = (context.evidence_bundle or {}).get("embedded_metadata", {})
    info, xmp = metadata.get("info", {}), metadata.get("xmp", {})
    proposals, attempts, assertions = [], [], []
    fields = [("Info.Author", [info.get("author", "")]),
              ("XMP.pdf:Author", xmp.get("pdf:Author", [])),
              ("XMP.dc:creator", xmp.get("dc:creator", []))]
    titles = [info.get("title", ""), *xmp.get("dc:title", [])]
    for field, values in fields:
        raw = "; ".join(values)
        if not raw.strip():
            continue
        author = _roster(raw)
        proposal = {"expert": "embedded_creator_proposal", "field": field,
                    "raw_credit": raw, "author": author,
                    "source": "embedded_metadata_proposal", "page": 0,
                    "eligible_for_selection": False}
        proposals.append(proposal)
        evidence = None
        names = _names(author)
        if len(names) == 1 and context.profile.work_scope != "whole_issue":
            surname = _words(names[0])[-1]
            for view in views:
                for sample in view.opening_pages:
                    if int(sample.get("page") or 0) != 1:
                        continue
                    text = str(sample.get("text") or "")
                    lines = [s.strip() for s in text.splitlines() if s.strip()]
                    byline = next((line for line in lines[:45]
                                   if _words(line) == _words(names[0])), "")
                    title = next((title for title in titles if len(_words(title)) >= 3
                                  and _contains(text[:5000], title)), "")
                    rights = re.search(r"(?:©|copyright)\s*(?:©\s*)?(?:19|20)\d{2}\s+"
                                       + re.escape(surname) + r"\b", text, re.I)
                    if not (byline and title and rights):
                        continue
                    index = lines.index(byline)
                    preceding = " ".join(lines[max(0, index - 2):index])
                    if re.search(r"\b(?:edited|reviewed|translated)\s+by\b", preceding, re.I):
                        continue
                    evidence = {"page": 1, "evidence": byline + " / " + title + " / " + rights[0],
                                "basis": "Visible opening name, matching work title, and named copyright corroborate the embedded proposal; secondary-role blocks remain separate"}
        if evidence:
            assertions.append({"author": author, "role": "author", "work": "selected work",
                               "family": "embedded_creator_with_visible_responsibility",
                               "priority": 102, "source": "text_metadata_corroborated_creator", **evidence})
        attempts.append({"expert": "embedded_visible_responsibility", "field": field,
                         "status": "assessed", "author": author,
                         "supported": bool(evidence), "reason": "visible work and role corroboration" if evidence
                         else "No complete visible responsibility corroboration; embedded credit is not authoritative"})

    bibliography = _bibliography(info.get("keywords", ""))
    if bibliography:
        proposals.append({"expert": "embedded_bibliography_proposal", **bibliography,
                          "source": "embedded_metadata_proposal", "page": 0,
                          "eligible_for_selection": False})
        matched = {}
        for view in views:
            for sample in view.opening_pages:
                text = str(sample.get("text") or "")
                # An actual publisher/export citation must contain the ordered
                # surname-first creator and parent title, not separate body hits.
                if (_contains(text, "ProQuest Ebook Central")
                        and _contains(text, bibliography["raw_credit"] + " " + bibliography["title"])):
                    matched[int(sample.get("page") or 0)] = text
        bundle = context.evidence_bundle or {}
        divider = (bundle.get("trace", {}).get("document_page_count") == 1
                   and any(re.search(r"(?im)^\s*part\s+[ivxlcdm]+\s*$", text)
                           and len(text.split()) < 140 for text in matched.values()))
        separate_contribution = any(
            re.fullmatch(r"\s*(?:foreword|afterword|translator.s (?:preface|note))\s*", line, re.I)
            for view in views for sample in view.opening_pages
            if int(sample.get("page") or 0) == 1
            for line in str(sample.get("text") or "").splitlines()[:15]
        )
        supported = ((len(matched) >= 2 or divider)
                     and context.profile.work_scope != "whole_issue"
                     and not separate_contribution)
        attempts.append({"expert": "embedded_parent_work_responsibility", "status": "assessed",
                         "author": bibliography["author"], "supported": supported,
                         "corroboration_pages": sorted(matched), "minimal_divider": divider,
                         "reason": "Printed parent-work export credit corroborates proposal" if supported
                         else "Embedded bibliography lacks sufficient printed work credit"})
        if supported:
            assertions.append({"author": bibliography["author"], "role": "author",
                               "work": "selected work", "page": min(matched),
                               "evidence": bibliography["raw_credit"] + " / " + bibliography["title"],
                               "family": "embedded_bibliography_with_printed_parent_credit",
                               "priority": 101, "source": "text_metadata_corroborated_creator",
                               "parent_title": bibliography["title"], "minimal_divider": divider,
                               "basis": "An authored parent export is corroborated on the actual pages; local contribution and secondary-role claims retain priority"})
    return proposals, attempts, assertions
