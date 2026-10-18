"""A provisional OCR guess cannot close a verified chapter investigation."""

from dataclasses import replace

import fitz
import pytest

from author_metadata.context import AuthorEvidenceContext
from author_metadata.dispatcher import infer_author
from author_metadata.publishing_acts import embedded_chapter, parent_editor_credit

pytestmark = pytest.mark.offline_deterministic


def chapter_context(tmp_path, *, matching_headers=2):
    path = tmp_path / "document.pdf"
    doc = fitz.open()
    contents = [
        "Collected Histories of Cultural Inquiry\nEdited by\nCecilia Brooks and Bernard Stone",
        "Copyright 2026 The Contributors\nEdited by Cecilia Brooks and Bernard Stone\nAll rights reserved. ISBN 9781234567890",
        "CHAPTER 7\nContested Public Memory\nAmelia Hart",
    ]
    for text in contents:
        page = doc.new_page()
        page.insert_text((60, 80), text, fontsize=13)
    for index in range(2):
        page = doc.new_page()
        header = (
            "Amelia Hart" if index < matching_headers else "Contested Public Memory"
        )
        page.insert_text((60, 40), header, fontsize=10)
        page.insert_text(
            (60, 160),
            "This paragraph discusses the evidence for the chapter's historical argument.",
            fontsize=11,
        )
    doc.save(path)
    samples = [{"page": i + 1, "text": page.get_text()} for i, page in enumerate(doc)]
    doc.close()
    context = AuthorEvidenceContext.from_samples(
        samples, path=path, title_hint="Collected Histories of Cultural Inquiry"
    )
    return replace(
        context,
        profile=replace(
            context.profile, kind="book", publication_type="book", work_scope="book"
        ),
    )


def test_editor_role_evidence_can_come_from_another_expert(tmp_path):
    context = chapter_context(tmp_path)
    provisional = {
        "author": "Ordinary Reporting Patterns",
        "source": "text_ocr_author_credit",
    }
    assert embedded_chapter(context, provisional) is None
    report = embedded_chapter(context, provisional, parent_editor_established=True)
    assert report["author"] == "Amelia Hart"
    assert report["corroboration_pages"] == [4, 5]


def test_all_expert_editor_evidence_enables_chapter_verification(tmp_path, monkeypatch):
    context = chapter_context(tmp_path)
    monkeypatch.setattr(
        "author_metadata.dispatcher._infer_before_collection",
        lambda _context: {
            "author": "Ordinary Reporting Patterns",
            "source": "text_ocr_author_credit",
            "evidence": "A provisional unrelated OCR interpretation",
        },
    )
    result = infer_author(context)
    assert result["author"] == "Amelia Hart"
    review = result["evidence_review"]
    assert any(
        c["author"] == "Amelia Hart" and c["work"] == "selected chapter"
        for c in review["claims"]
    )
    assert any(
        a.get("expert") == "local_chapter_boundary"
        and a.get("parent_editor_evidence_seen")
        and a.get("author") == "Amelia Hart"
        for a in review["expert_attempts"]
    )
    assert any(
        c.get("role") == "editor" and c.get("work") == "parent book"
        for c in review["claims"]
    )


@pytest.mark.parametrize("matching_headers", [0, 1])
def test_parent_editor_evidence_is_not_itself_a_chapter_verdict(
    tmp_path, matching_headers
):
    context = chapter_context(tmp_path, matching_headers=matching_headers)
    assert (
        embedded_chapter(
            context,
            {"source": "text_ocr_author_credit"},
            parent_editor_established=True,
        )
        is None
    )


def test_whole_issue_remains_outside_local_chapter_override(tmp_path):
    context = chapter_context(tmp_path)
    context = replace(
        context, profile=replace(context.profile, work_scope="whole_issue")
    )
    assert embedded_chapter(context, {}, parent_editor_established=True) is None


def test_missing_role_evidence_is_not_promoted_to_parent_editor():
    assert not parent_editor_credit(
        {"source": "text_ocr_author_credit", "evidence": "Amelia Hart"}
    )
    assert parent_editor_credit(
        {"source": "text_strict_credit_block", "evidence": "Edited by Cecilia Brooks"}
    )
