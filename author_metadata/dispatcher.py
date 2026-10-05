"""Select one publication-specific author strategy from visible PDF evidence."""

from pathlib import Path

from .book import infer as infer_book_author
from .context import AuthorEvidenceContext
from .filename import infer_author_from_filename
from .legacy_fallback import (
    infer_author_from_samples_or_filename as infer_generic_author,
    infer_author_from_text_samples as infer_generic_text_author,
)
from .report import infer_report_author
from .review import infer_review_author
from .scholarly_article import infer as infer_scholarly_author
from .web_article import infer_web_article_author


_STRATEGIES = {
    "book": infer_book_author,
    "report": infer_report_author,
    "review": infer_review_author,
    "scholarly_article": infer_scholarly_author,
    "web_article": infer_web_article_author,
}


def infer_author(context: AuthorEvidenceContext):
    """A classified document is assessed only under its own role grammar."""
    strategy = _STRATEGIES.get(context.profile.kind)
    if strategy is None:
        if context.path is None:
            return infer_generic_text_author(context.samples, title_hint=context.title_hint)
        return infer_generic_author(context.samples, context.path, title_hint=context.title_hint)
    report = strategy(context)
    if report.get("author"):
        return report
    # An explicit catalog byline is independent evidence for a work's creator,
    # but generic tail/name-shape guesses must not override a classified role.
    if context.path is not None:
        filename = infer_author_from_filename(context.path, title_hint=context.title_hint)
        if filename.get("source") in {"filename_explicit_byline", "filename_leading_names"}:
            return filename
    return report


def infer_author_from_samples(samples, path: Path, title_hint=""):
    return infer_author(AuthorEvidenceContext.from_samples(samples, path=path, title_hint=title_hint))
