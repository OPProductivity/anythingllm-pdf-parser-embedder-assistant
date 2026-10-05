"""Resolve accepted document identity from metadata and author evidence."""
import re
from pathlib import Path
from rag_pdf_tools import normalize_text
from .constants import TRUSTED_AUTHOR_INFERENCE_SOURCES
from .names import normalize_author_candidate, author_candidate_is_font_metadata, looks_like_person_name


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
    # A source-layout/office extension in Title metadata is a strong sign of
    # an internal production filename, including short names such as
    # ``article.indd`` that would otherwise look superficially harmless.
    if re.search(r"\.(?:pdf|indd|indb|idml|docx?|pptx?|xlsx?|txt|rtf|html?|xml)$", title, flags=re.I):
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
    if inferred_value and inferred_source in TRUSTED_AUTHOR_INFERENCE_SOURCES:
        return {"author": inferred_value, "source": inferred_source or "text_inference"}
    return {"author": "", "source": "not_available"}
