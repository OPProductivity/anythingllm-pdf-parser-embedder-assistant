"""PDF-page acquisition and selected-text recovery for author evidence."""

import re
from pathlib import Path

import fitz
from rag_pdf_tools import normalize_text

from .constants import POST_EXTRACTION_AUTHOR_TRUSTED_SOURCES
from .context import AuthorEvidenceContext
from .dispatcher import infer_author
from .identity import resolve_author_from_metadata_and_inference
from .names import (looks_like_person_name, normalize_author_candidate,
                    split_author_line_candidates)
from .work_identity import resolve_work_identity


def selected_extraction_author_samples(pages, *, page_limit=4):
    """Use only nonempty physical pages from the chosen extraction candidate."""
    limit = max(1, min(6, int(page_limit or 4)))
    selected = []
    seen_pages = set()
    for row in pages or []:
        if not isinstance(row, dict):
            continue
        try:
            page = int(row.get("page") or 0)
        except (TypeError, ValueError):
            continue
        value = str(row.get("text") or "")
        if page <= 0 or page in seen_pages or not normalize_text(value):
            continue
        seen_pages.add(page)
        selected.append({"page": page, "text": value})
        if len(selected) >= limit:
            break
    return selected


def recover_author_from_selected_extraction(pages, *, title_hint="", page_limit=4, profile=None):
    """Recover only a trusted visible credit after OCR/backend selection."""
    samples = selected_extraction_author_samples(pages, page_limit=page_limit)
    if not samples:
        return {"author": "", "source": "not_assessed_no_selected_opening_text",
                "page": 0, "evidence": "", "sample_pages": []}
    context = AuthorEvidenceContext.from_samples(samples, title_hint=title_hint, profile=profile)
    report = dict(infer_author(context))
    if report.get("source") not in POST_EXTRACTION_AUTHOR_TRUSTED_SOURCES and report.get("source") != "conflicting_work_credits":
        extra_catalog_page = selected_extraction_author_samples(pages, page_limit=5)[4:5]
        for sample in [*samples, *extra_catalog_page]:
            if sample["page"] > 5:
                continue
            compact = " ".join(str(sample["text"]).split())
            # OCR sometimes emits the complete title and its explicit By
            # credit on one line instead of preserving the title-page break.
            titlepage = re.match(r"^(.{20,180}?)\s+[Bb]y\s+(.{5,140})$", compact)
            if sample["page"] <= 4 and titlepage and len(titlepage.group(1).split()) >= 5:
                names = split_author_line_candidates(
                    titlepage.group(2), allow_all_caps=True, require_complete=True,
                )
                if names:
                    report = {"author": ", ".join(names), "source": "text_byline",
                              "page": sample["page"], "evidence": compact[:250]}
                    break
            # A catalog card names the writer twice: surname-first in the
            # heading and given-name-first after the work title's slash.
            catalog = re.search(
                r"Library of Congress Cataloging-in-Publication Data\s+"
                r"([^,.;]{2,45}),\s*([^,.;]{2,45}),\s*\d{4}[^/]{10,220}/\s*"
                r"([^.;]{5,90})\.", compact, re.I,
            )
            if catalog:
                heading = normalize_author_candidate(
                    f"{catalog.group(2)} {catalog.group(1)}")
                printed = normalize_author_candidate(catalog.group(3))
                if (heading.casefold() == printed.casefold()
                        and looks_like_person_name(printed)):
                    report = {"author": printed, "source": "text_strict_credit_block",
                              "page": sample["page"], "evidence": catalog.group(0)[:250]}
                    if sample in extra_catalog_page:
                        samples = [*samples, sample]
                    break
    if not report.get("author") and report.get("source") == "not_found":
        first = samples[0]
        lines = [normalize_text(line) for line in first["text"].splitlines() if normalize_text(line)]
        for index, line in enumerate(lines[:28]):
            if not re.fullmatch(r"[A-ZÀ-ÖØ-Þ][A-ZÀ-ÖØ-Þ '’-]{4,70}", line):
                continue
            name = normalize_author_candidate(line)
            heading = " ".join(lines[max(0, index - 3):index])
            if (not looks_like_person_name(name, allow_all_caps=True)
                    or not 2 <= len(name.split()) <= 4
                    or len(heading.split()) < 5 or not heading.isupper()):
                continue
            repeated = any(re.search(rf"(?im)^\s*\d*\s*{re.escape(line)}\s*$", row["text"])
                           for row in samples[1:] if row["page"] != first["page"])
            if repeated:
                report = {"author": name, "source": "text_compact_caps_byline",
                          "page": first["page"],
                          "evidence": "opening title byline corroborated by running head"}
                break
    report["document_profile"] = context.profile_evidence()
    report["sample_pages"] = [row["page"] for row in samples]
    base_source = str(report.get("source") or "not_found")
    if report.get("author") and base_source not in POST_EXTRACTION_AUTHOR_TRUSTED_SOURCES:
        report.update({"author": "", "source": f"selected_extraction_rejected_weak_{base_source}",
                       "evidence": ""})
        return report
    report["source"] = f"selected_extraction_{base_source}"
    return report


def corroborates_filename_surname(existing_author, existing_source, native_samples, report):
    """Allow a scan's explicit first-page byline to expand a filename surname."""
    surname = normalize_text(existing_author or "")
    name = normalize_text(report.get("author") or "")
    if (existing_source != "filename_leading_surname" or len(surname.split()) != 1
            or any(normalize_text(row.get("text") or "") for row in native_samples or [])
            or report.get("source") != "selected_extraction_text_byline"
            or report.get("page") != 1 or not 2 <= len(name.split()) <= 4
            or not looks_like_person_name(name)
            or any(mark in name for mark in (",", ";", "&"))
            or " and " in name.casefold()):
        return False
    return name.split()[-1].casefold() == surname.casefold()


def infer_author_from_initial_pdf_pages(path: Path, title_hint="", *, page_limit=3,
                                        use_file_title_fallback=True):
    """Fast UI suggestion, with no OCR or persisted source-text sample."""
    pdf_path = Path(path)
    try:
        with fitz.open(pdf_path) as doc:
            metadata = dict(doc.metadata or {})
            samples = []
            limit = min(max(1, int(page_limit or 3)), len(doc))
            for index in range(limit):
                value = doc.load_page(index).get_text("text")
                if value:
                    samples.append({"page": index + 1, "text": value})
            identity = resolve_work_identity(
                metadata.get("title") or "", pdf_path, samples,
                title_override=title_hint,
                use_file_title_fallback=use_file_title_fallback,
            )
            if limit >= 4 and identity.profile.kind == "book":
                for index in range(limit, min(12, len(doc))):
                    value = doc.load_page(index).get_text("text")
                    if value:
                        samples.append({"page": index + 1, "text": value})
    except Exception as exc:
        return {"author": "", "source": "error", "page": 0, "evidence": "",
                "error": type(exc).__name__}
    identity = resolve_work_identity(
        metadata.get("title") or "", pdf_path, samples,
        title_override=title_hint,
        use_file_title_fallback=use_file_title_fallback,
    )
    report = infer_author(AuthorEvidenceContext.from_samples(
        samples, path=pdf_path, title_hint=identity.title, profile=identity.profile,
    ))
    resolved = resolve_author_from_metadata_and_inference(metadata.get("author") or "", report)
    return {"author": resolved["author"], "source": resolved["source"],
            "page": int(report.get("page") or 0), "evidence": str(report.get("evidence") or ""),
            "work_identity": identity.as_evidence()}


def infer_author_from_pdf_text(path: Path, title_hint=""):
    """Read a small standard PDF sample for a manual metadata evaluation."""
    path = Path(path)
    try:
        with fitz.open(path) as doc:
            numbers = list(dict.fromkeys(number for number in
                        (1, 2, 3, 4, max(1, len(doc) - 1), len(doc))
                        if 1 <= number <= len(doc)))
            samples = []
            for number in numbers:
                value = doc.load_page(number - 1).get_text("text")
                if value:
                    samples.append({"page": number, "text": value})
        return infer_author(AuthorEvidenceContext.from_samples(samples, path=path, title_hint=title_hint))
    except Exception as exc:
        return {"author": "", "source": "error", "page": 0, "evidence": str(exc)}
