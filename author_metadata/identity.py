"""Resolve accepted document identity from metadata and author evidence."""
import re
from pathlib import Path
from rag_pdf_tools import normalize_text
from .constants import TRUSTED_AUTHOR_INFERENCE_SOURCES
from .names import (normalize_author_candidate, author_candidate_is_document_role,
                    author_candidate_is_font_metadata, looks_like_person_name)


def normalize_metadata_author(value):
    """Keep only person-shaped author metadata, normalizing common exports.

    PDF metadata is an uncontrolled field: it frequently contains a login,
    a publisher, or semicolon-separated surname-first names.  This is a
    confidence filter, not a source-specific allowlist; collective credits
    deliberately fall through to stronger visible evidence or a title fallback.
    """
    raw = normalize_text(value or "")
    if not raw:
        return ""
    pieces = [piece for piece in re.split(r"\s*[;|]\s*", raw) if piece.strip()]
    normalized = []
    for piece in pieces:
        candidate = normalize_author_candidate(piece)
        if author_candidate_is_font_metadata(candidate):
            continue
        if looks_like_person_name(candidate) and candidate not in normalized:
            normalized.append(candidate)
    return ", ".join(normalized)


def normalize_metadata_title(value):
    """Accept a human-facing PDF title, rejecting common producer leftovers.

    PDF title metadata is often a publishing-workstation filename rather than
    a document title (for example ``32(8) Theobald.indd``).  Keeping such a
    value is worse than a clean selected filename: it pollutes the editable UI
    title, short label, and automatic workspace suggestion.  This is a narrow
    quality filter, not a title inference system; readable metadata remains
    preferred whenever it is plausibly intended for people.
    """
    title = normalize_text(value or "")
    if len(title) < 3 or len(title) > 300 or any(ord(character) < 32 for character in title):
        return ""
    lowered = title.casefold().strip()
    if re.fullmatch(
        r"(?:untitled(?: document)?|new document|document(?: \d+)?|scan(?: \d+)?|"
        r"some[ _-]?title|document[ _-]?title|pdf[ _-]?title|no job name)",
        lowered,
    ):
        return ""
    if re.fullmatch(r"OP-[A-Z]+\d+\s+\d+\.\.\d+", title):
        return ""
    # A source-layout/office/raster extension in Title metadata is a strong sign of
    # an internal production filename, including short names such as
    # ``article.indd`` that would otherwise look superficially harmless.
    if re.search(r"\.(?:pdf|indd|indb|idml|docx?|pptx?|xlsx?|txt|rtf|html?|xml|png|jpe?g|jpe|jfif|tiff?|bmp|gif|webp|heic|heif)$", title, flags=re.I):
        return ""
    return title


def resolve_title_from_metadata_or_filename(
    metadata_title,
    path: Path,
    *,
    title_override="",
    use_file_title_fallback=True,
):
    """Resolve one display title using the same rule in UI and preparation."""
    override = normalize_text(title_override or "")
    if override:
        return {"title": override, "source": "user_override"}
    metadata_value = normalize_metadata_title(metadata_title)
    if metadata_value:
        return {"title": metadata_value, "source": "pdf_metadata"}
    filename_value = normalize_text(Path(path).stem)
    catalog_parts = filename_value.split("--")
    if len(catalog_parts) >= 3 and looks_like_person_name(
        catalog_parts[1].strip(), title_hint=catalog_parts[0], allow_all_caps=True,
    ):
        filename_value = normalize_text(catalog_parts[0])
    if use_file_title_fallback and filename_value:
        return {"title": filename_value, "source": "filename_fallback"}
    return {"title": "Untitled PDF", "source": "generated_placeholder"}


def resolve_author_from_metadata_and_inference(metadata_author, inference, *, author_override=""):
    """Resolve one document author without trusting embedded PDF Author data.

    ``metadata_author`` remains in this compatibility signature because old
    callers and audit tools still pass it. It is deliberately diagnostic-only:
    durable author identity comes from a user override or trusted visible-text
    and explicit filename evidence.
    """
    override = normalize_text(author_override or "")
    if override:
        return {"author": override, "source": "user_override"}
    _ = metadata_author
    report = dict(inference or {})
    inferred_value = normalize_text(report.get("author") or "")
    inferred_source = str(report.get("source") or "")
    surname_only_sources = {
        "filename_leading_surname", "filename_leading_surnames",
        "filename_surname_before_year", "filename_surname_before_section_number",
    }
    single_person_credit = (
        inferred_source != "text_corporate_cover_byline"
        and inferred_source not in surname_only_sources
        and not re.search(r",|;|\s+and\s+|&", inferred_value, re.I)
    )
    if (inferred_value and inferred_source in TRUSTED_AUTHOR_INFERENCE_SOURCES
            and not author_candidate_is_document_role(inferred_value)
            and (not single_person_credit or looks_like_person_name(inferred_value))):
        return {"author": inferred_value, "source": inferred_source or "text_inference"}
    return {"author": "", "source": "not_available"}


def author_confidence(resolved_author):
    """Return a reusable evidence grade, not a calibrated probability.

    Consumers can compare grades without parsing source names. Zero means
    unresolved; higher values reflect the *kind* of accepted evidence.
    """
    author = normalize_text((resolved_author or {}).get("author") or "")
    source = str((resolved_author or {}).get("source") or "")
    if not author or source == "not_available":
        score, basis = 0, "unresolved"
    elif source == "user_override":
        score, basis = 100, "user_override"
    elif source == "filename_pdf_credit_match":
        score, basis = 90, "filename_and_visible_author_field"
    elif source == "filename_pdf_standalone_match":
        score, basis = 82, "filename_and_opening_byline"
    elif source == "grobid_header_author":
        score, basis = 85, "grobid_header_and_visible_byline"
    elif source in {"text_author_label", "text_bibliographic_author_label",
                    "text_thesis_titlepage_author", "text_byline", "text_written_by"}:
        score, basis = 90, "explicit_visible_credit"
    elif source.startswith("text_") or source.startswith("selected_extraction_text_"):
        score, basis = 80, "visible_text_inference"
    elif source.startswith("filename_"):
        score, basis = 70, "structured_filename"
    else:
        score, basis = 0, "unresolved"
    if 0 < score < 100 and (resolved_author or {}).get("grobid_agrees"):
        score, basis = max(score, 92), "visible_credit_and_grobid_header_agreement"
    level = "high" if score >= 90 else "moderate" if score >= 70 else "unresolved"
    return {"score": score, "level": level, "basis": basis}
