"""Opt-in real-PDF identity checks; fixture PDFs stay outside the repository."""

import hashlib
import json
import os
from pathlib import Path
import unicodedata

import pytest

from auto_anythingllm_pipeline import pdf_metadata
from author_metadata.context import AuthorEvidenceContext
from author_metadata.dispatcher import infer_author
from author_metadata.identity import resolve_author_from_metadata_and_inference
from author_metadata.pdf import infer_author_from_initial_pdf_pages
from author_metadata.title_evidence import words
from author_metadata.work_identity import resolve_work_identity


LABELS = json.loads(
    (
        Path(__file__).parent / "fixtures" / "public_pdf_work_identity_labels.json"
    ).read_text(encoding="utf-8")
)
ROOT = Path(
    os.environ.get(
        "ANYTHINGLLM_PUBLIC_PDF_CORPUS",
        Path.home() / "Downloads" / "_anythingllm-public-metadata-corpus-20261005",
    )
)

pytestmark = pytest.mark.offline_deterministic


@pytest.mark.parametrize("item", LABELS["items"], ids=lambda item: item["file"])
def test_real_pdf_work_scope_and_no_false_credit(item):
    if not ROOT.is_dir():
        pytest.skip("Public PDF corpus is not installed on this machine")
    path = ROOT / item["file"]
    assert path.is_file(), f"Missing hash-bound PDF: {path}"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"]

    metadata = pdf_metadata(path, include_author_samples=True)
    assert metadata["pdf_page_count"] == item["pages"]
    assert not metadata.pop("_author_sample_error")
    samples = metadata.pop("_author_text_samples")
    identity = resolve_work_identity(metadata.get("title"), path, samples)
    context = AuthorEvidenceContext.from_samples(
        samples,
        path=path,
        title_hint=identity.title,
        profile=identity.profile,
    )
    inference = infer_author(context)
    author = resolve_author_from_metadata_and_inference(
        metadata.get("author"), inference
    )["author"]

    # Correctly unresolved is preferable to a role-confused nonempty credit.
    expected = ", ".join((item.get("creator") or "").split("; "))
    # Visible forms were checked on the pages; a surname-only opinion credit
    # is explicitly partial and must never be reported as an expanded name.
    visible_forms = {expected, *item.get("accepted_visible_forms", [])}
    assert not author or author in visible_forms

    if item["category"] == "historical_newspaper_issue":
        assert identity.profile.kind == "periodical_issue"
        assert identity.profile.work_scope == "whole_issue"
        assert not author
    elif item["category"] in {"edited_book_chapter", "coordinated_book_chapter"}:
        assert identity.profile.kind == "book_chapter"
        assert identity.profile.work_scope == "chapter"
        if item["category"] == "coordinated_book_chapter":
            # The complete printed contributor roster is now recoverable.
            assert author == expected
    elif item["category"] == "court_opinion":
        assert identity.profile.kind == "legal_opinion"
        assert author == "Sotomayor"
        assert inference["creator_type"] == "person_surname"
    elif item["category"] in {"government_report", "policy_brief"}:
        assert identity.profile.kind == "report"
        if item["category"] == "government_report":
            assert author == "United States Government Accountability Office"
            assert inference["creator_type"] == "organization"
    elif item["category"] in {"academic_book_review_blog", "book_review_in_newspaper"}:
        assert identity.profile.kind == "review"
    elif item["category"] in {
        "news_article",
        "broadcast_news_article",
        "science_news",
        "personal_blog",
        "technical_tutorial_blog",
        "expert_opinion_blog",
        "corporate_blog",
        "investigative_news",
    }:
        assert identity.profile.kind == "web_article"

    if identity.container_title and item.get("work_title"):
        assert words(identity.title) == words(item["work_title"])

    preview = infer_author_from_initial_pdf_pages(path, page_limit=4)
    if preview["author"] != author:
        # The UI preview may add a GROBID header credit after the native
        # metadata route abstains. Require an independently reviewed work
        # author rather than assuming both evidence paths must be identical.
        def credit_key(value):
            folded = unicodedata.normalize("NFKD", value).casefold()
            return "".join(char for char in folded if char.isalnum())

        assert preview["grobid"]["decision"] in {
            "filled_unknown",
            "expanded_filename_surname",
        }
        assert credit_key(preview["author"]) in {credit_key(v) for v in visible_forms}
    assert preview["work_identity"]["category"] == identity.profile.kind
