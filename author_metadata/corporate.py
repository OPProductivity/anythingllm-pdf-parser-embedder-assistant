"""High-specificity corporate publication cues and work-level person credits."""

import re
import unicodedata

from rag_pdf_tools import normalize_text

from .genre_evidence import credited_names, result


def _opening(samples):
    ordered = []
    for sample in samples or []:
        if not isinstance(sample, dict):
            continue
        try:
            page = int(sample.get("page") or 0)
        except (TypeError, ValueError):
            continue
        if 1 <= page <= 4:
            ordered.append((page, sample))
    return [unicodedata.normalize("NFKC", str(sample.get("text") or ""))
            for _, sample in sorted(ordered, key=lambda item: item[0])]


def corporate_cue(samples):
    """Return a subtype only when several independent publication cues agree."""
    opening = _opening(samples)
    if not opening:
        return None
    cover = opening[0]
    head = "\n".join(opening[:3])
    if re.search(r"\bFORM\s+10-K\b", cover, re.I) and re.search(
        r"Exact name of registrant as specified", cover, re.I
    ):
        return "corporate_filing", "sec_form_and_registrant"

    headline = re.search(
        r"(?im)^[A-Z][A-Za-z0-9&.,'’\- ]{1,70}\s+Reports?\s+"
        r"(?:Fourth|First|Second|Third|Q[1-4]|Record|Financial|Fiscal|Full\s+Year)",
        cover[:2200],
    )
    financial_body = re.search(
        r"\b(?:revenues?|net income|financial results|earnings per share|EPS)\b",
        cover[:3200], re.I,
    )
    release_label = re.search(r"\b(?:Earnings Release|For Immediate Release)\b", cover[:1800], re.I)
    ticker_report = re.search(
        r"\b(?:NASDAQ|NYSE)\s*:\s*[A-Z]{1,6}\b.{0,120}\btoday\s+(?:reported|announced)\b",
        cover[:2800], re.I | re.S,
    )
    datelined_report = re.search(
        r"\b[A-Z]{3,},\s+[A-Z][a-z]+\.?\s+\d{1,2},\s*20\d{2}\s*[–-]\s*"
        r"[^\n]{3,120}?\btoday\s+reported\b",
        cover[:2800], re.I,
    )
    if headline and financial_body and (release_label or ticker_report or datelined_report):
        return "corporate_release", "financial_results_release_and_datelined_issuer"

    customer_case = (
        re.search(r"(?im)^\s*Challenge\s*$", cover)
        and re.search(r"(?im)^\s*Summary\s*$", cover)
        and re.search(r"(?im)^\s*INDUSTRY\s*:", cover)
        and re.search(r"\b(?:customer|client|employees?)\b", cover, re.I)
        and re.search(r"©\s*[^\n]{3,70}\s+All rights reserved", cover, re.I)
    )
    if customer_case:
        return "corporate_client_case", "case_structure_and_publisher_copyright"

    copyright_issuer = re.search(
        r"(?im)^\s*(?:copyright\s*©?\s*|©\s*)20\d{2}\s+[A-Z][^\n]{2,70}",
        head[:5000],
    )
    results_cover = re.search(r"\b(?:financial\s+results|quarter\s+performance)\b", cover, re.I)
    financial_followup = re.search(r"\b(?:financial results|non-GAAP financial measures|income statement)\b",
                                 head[:5000], re.I)
    if copyright_issuer and results_cover and financial_followup:
        return "corporate_presentation", "results_cover_and_copyright"

    annual = re.search(r"\b(?:20\d{2}\s+Annual\s+Report|Annual\s+Report\s+20\d{2})\b", cover, re.I)
    repeated_issuer = re.search(
        r"(?im)^[A-Z][A-Za-z&. ]{1,45}\s+20\d{2}\s+Annual\s+Report\b", head
    )
    if annual and repeated_issuer:
        return "corporate_annual_report", "annual_report_and_issuer_running_title"

    if re.match(r"^\s*Company\s+Overview\b", cover, re.I) and re.search(
        r"(?m)^[A-Z][A-Z&. -]{2,35}\s*\nHeadquarters:\s*", head
    ):
        return "corporate_presentation", "company_overview_and_company_fact_page"
    return None


def infer(context):
    cover = _opening(context.opening_pages)
    if cover:
        lines = [normalize_text(line) for line in cover[0].splitlines() if normalize_text(line)]
        for index, line in enumerate(lines[:18]):
            match = re.fullmatch(r"(?:by|written by|authored by)\s*:?[ ]+(.+)", line, re.I)
            if match:
                if any(re.search(r"\bcontact\b", previous, re.I)
                       for previous in lines[max(0, index - 3):index]):
                    continue
                if (context.profile.publication_type == "corporate_release"
                        and not any(re.search(r"\bReports?\b.*\b(?:Results|Revenue)\b", previous, re.I)
                                    for previous in lines[:index])):
                    continue
                names = credited_names(match.group(1))
                if names:
                    return result(context, names, "text_corporate_cover_byline", 1, line)
    return {"author": "", "source": "corporate_work_author_not_resolved", "page": 0,
            "evidence": "no explicit cover work-level person credit"}
