"""Select one publication-specific author strategy from visible PDF evidence."""

from pathlib import Path
import re

from .book import infer as infer_book_author
from .book_chapter import infer as infer_book_chapter_author
from .bibliographic import infer_repeated_platform_credit
from .corporate import infer as infer_corporate_author
from .context import AuthorEvidenceContext
from .constants import TRUSTED_AUTHOR_INFERENCE_SOURCES
from .credit_match import (
    match_filename_to_pdf_author_credit,
    match_filename_to_scholarly_byline,
)
from .filename import infer_author_from_filename
from .frontmatter import infer_generic_frontmatter
from .legacy_fallback import (
    infer_author_from_samples_or_filename as infer_generic_author,
    infer_author_from_text_samples as infer_generic_text_author,
)
from .report import infer_report_author
from .review import infer_review_author
from .scholarly_article import infer as infer_scholarly_author
from .scope import infer_issue_author, infer_legal_opinion_author
from .thesis import infer as infer_thesis_author
from .title_evidence import words
from .web_article import infer_web_article_author
from .structured_credit import (
    infer as infer_structured_credit,
    credential_roster,
    positioned_opening,
    document_record,
    publisher_titlepage,
    remove_translator_role,
    biographic_titlepage,
    work_rights_credit,
    whole_book_editors,
    continued_byline,
)
from .bounded_ocr import infer as infer_bounded_ocr
from .portable_layout import infer as infer_portable_layout
from .portable_book import infer as infer_portable_book
from .native_article import infer as infer_native_article


_STRATEGIES = {
    "book": infer_book_author,
    "report": infer_report_author,
    "review": infer_review_author,
    "scholarly_article": infer_scholarly_author,
    "web_article": infer_web_article_author,
    "book_chapter": infer_book_chapter_author,
    "periodical_issue": infer_issue_author,
    "legal_opinion": infer_legal_opinion_author,
    "thesis_dissertation": infer_thesis_author,
    "corporate_publication": infer_corporate_author,
}


def _infer_author(context: AuthorEvidenceContext):
    """A classified document is assessed only under its own role grammar."""
    roster = credential_roster(context)
    if roster.get("author"):
        return roster
    strategy = _STRATEGIES.get(context.profile.kind)
    if strategy is None:
        if context.path is None:
            return infer_generic_text_author(
                context.samples, title_hint=context.title_hint
            )
        platform = infer_repeated_platform_credit(context)
        if platform.get("author"):
            return platform
        report = infer_generic_author(
            context.samples, context.path, title_hint=context.title_hint
        )
        if not report.get("author") or str(report.get("source", "")).startswith(
            "filename_"
        ):
            ocr = infer_bounded_ocr(context)
            if ocr.get("author"):
                return ocr
        matched = match_filename_to_pdf_author_credit(context)
        if matched and (
            report.get("source") not in TRUSTED_AUTHOR_INFERENCE_SOURCES
            or str(report.get("author") or "").casefold()
            == matched["author"].casefold()
        ):
            return matched
        if (
            not report.get("author")
            or report.get("source") not in TRUSTED_AUTHOR_INFERENCE_SOURCES
        ):
            frontmatter = infer_generic_frontmatter(context)
            if frontmatter.get("author"):
                return frontmatter
        if (
            not report.get("author")
            or report.get("source") not in TRUSTED_AUTHOR_INFERENCE_SOURCES
        ):
            structured = infer_structured_credit(context)
            if structured.get("author"):
                return structured
        return report
    report = strategy(context)
    if report.get("author"):
        return report
    if context.profile.kind == "book_chapter":
        platform = infer_repeated_platform_credit(context)
        if platform.get("author"):
            return platform
    matched = match_filename_to_pdf_author_credit(context)
    if matched:
        return matched
    matched = match_filename_to_scholarly_byline(context)
    if matched:
        return matched
    structured = infer_structured_credit(context)
    if structured.get("author"):
        return structured
    positioned = positioned_opening(context)
    if positioned.get("author"):
        return positioned
    ocr = infer_bounded_ocr(context)
    if ocr.get("author"):
        return ocr
    if report.get("source") != "not_found":
        return report
    if context.profile.kind == "book" and context.path is not None:
        opening = infer_generic_author(
            context.opening_pages, context.path, title_hint=context.title_hint
        )
        if opening.get("author") and opening.get("source") in {
            "text_edited_by",
            "text_titlepage_publisher_byline",
            "filename_structured_corroborated_text_names",
        }:
            return opening
        if (
            opening.get("author")
            and opening.get("source") == "text_opening_title_block_byline"
            and "editors" in str(opening.get("evidence") or "").casefold()
        ):
            return opening
        if (
            opening.get("author")
            and opening.get("source") == "text_title_adjacent_byline"
            and "foreword_cover" in context.profile.cues
        ):
            return opening
    if context.profile.kind == "scholarly_article" and not any(
        "to cite this article:" in str(s.get("text") or "").casefold()
        for s in context.opening_pages
    ):
        expected = words(context.title_hint)
        visible = words(
            " ".join(
                str(s.get("text") or "")[:1800]
                for s in context.opening_pages
                if int(s.get("page") or 0) == 1
            )
        )
        title_corroborated = len(expected) >= 5 and any(
            visible[pos : pos + 5] == expected[start : start + 5]
            for start in range(len(expected) - 4)
            for pos in range(max(0, len(visible) - 4))
        )
        first_text = next(
            (
                str(s.get("text") or "")
                for s in context.opening_pages
                if int(s.get("page") or 0) == 1
            ),
            "",
        )
        journal_volume = bool(
            re.search(
                r"\bVol\.?\s*\d+\s*,?\s*No\.?\s*\d+|\b[A-Z][A-Za-z ]{4,60}\s+\d{1,3}\(\d+\)",
                first_text[:1600],
                re.I,
            )
        )
        if title_corroborated or journal_volume:
            opening = infer_generic_text_author(
                context.opening_pages, title_hint=context.title_hint
            )
            if (
                opening.get("author")
                and opening.get("source")
                in {
                    "text_opening_title_block_byline",
                    "text_title_window_adjacent_byline",
                    "text_title_adjacent_byline",
                    "text_affiliated_byline",
                    "text_strict_credit_block",
                }
                and (
                    title_corroborated
                    or (
                        journal_volume
                        and opening.get("source")
                        in {
                            "text_affiliated_byline",
                            "text_opening_title_block_byline",
                        }
                        and ("," in opening["author"] or ";" in opening["author"])
                    )
                )
            ):
                return opening
    if context.profile.kind == "web_article" and context.path is not None:
        opening = infer_generic_author(
            context.opening_pages, context.path, title_hint=context.title_hint
        )
        if (
            opening.get("author")
            and opening.get("source") == "text_visible_title_person_prefix"
        ):
            return opening
    # An explicit catalog byline is independent evidence for a work's creator,
    # but generic tail/name-shape guesses must not override a classified role.
    if context.path is not None:
        filename = infer_author_from_filename(
            context.path, title_hint=context.title_hint
        )
        if filename.get("source") in {
            "filename_explicit_byline",
            "filename_leading_names",
        }:
            return filename
    return report


def infer_author(context: AuthorEvidenceContext):
    labelled = document_record(context)
    if labelled.get("author"):
        return labelled
    article = infer_native_article(context)
    if article.get("author"):
        return article
    report = _infer_author(context)
    book = infer_portable_book(context)
    if book.get("author") and (
        not report.get("author")
        or str(report.get("source", "")).startswith("filename_")
        or set(words(report["author"])) < set(words(book["author"]))
    ):
        report = book
    layout = infer_portable_layout(context)
    if layout.get("author"):
        old = set(words(report.get("author", "")))
        new = set(words(layout["author"]))
        heading = set(words(str(layout.get("evidence", "")).split(" / ", 1)[0]))
        if (
            not old
            or str(report.get("source", "")).startswith("filename_")
            or report.get("source") not in TRUSTED_AUTHOR_INFERENCE_SOURCES
            or (new < old and old - new <= heading)
        ):
            report = layout
    continued = continued_byline(context)
    if continued.get("author"):
        report = continued
    if not report.get("author") or str(report.get("source", "")).startswith(
        "filename_"
    ):
        editors = whole_book_editors(context)
        if editors.get("author"):
            report = editors
    biography = biographic_titlepage(context)
    if biography.get("author") and (
        not report.get("author")
        or set(words(report["author"])).issubset(set(words(biography["author"])))
    ):
        report = biography
    if (
        not report.get("author")
        or report.get("source") not in TRUSTED_AUTHOR_INFERENCE_SOURCES
    ):
        rights = work_rights_credit(context)
        if rights.get("author"):
            report = rights
    if (
        not report.get("author")
        or report.get("source") not in TRUSTED_AUTHOR_INFERENCE_SOURCES
    ):
        positioned = positioned_opening(context)
        if positioned.get("author"):
            report = positioned
    if (
        not report.get("author")
        or report.get("source") not in TRUSTED_AUTHOR_INFERENCE_SOURCES
        or report.get("source") == "text_bibliographic_author_label"
        or str(report.get("source", "")).startswith("filename_")
    ):
        titlepage = publisher_titlepage(context)
        if titlepage.get("author"):
            report = titlepage
        else:
            biography = biographic_titlepage(context)
            if biography.get("author"):
                report = biography
    return remove_translator_role(report, context)


def infer_author_from_samples(samples, path: Path, title_hint=""):
    return infer_author(
        AuthorEvidenceContext.from_samples(samples, path=path, title_hint=title_hint)
    )
