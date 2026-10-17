"""A positive guess cannot suppress remaining evidence or role experts."""

from pathlib import Path
from types import SimpleNamespace
import subprocess
import fitz
import pytest
from author_metadata.context import AuthorEvidenceContext
from author_metadata.profile import DocumentProfile
from author_metadata.evidence_collection import collect, _CACHE
from author_metadata.publishing_relations import claims, select

pytestmark = pytest.mark.offline_deterministic


def ctx(samples=(), **kw):
    return AuthorEvidenceContext.from_samples(
        samples, profile=DocumentProfile("book", (), ()), **kw
    )


@pytest.mark.parametrize(
    "wording",
    [
        "The right of Amina Rao to be identified as author of this work has been asserted.",
        "The rights of Amina Rao as author have been asserted.",
        "Amina Rao has asserted her moral right to be identified as the author of this book.",
        "The moral rights of Amina Rao as author have been asserted.",
    ],
)
def test_responsibility_wording_family(wording):
    found, _ = claims(
        [{"page": 5, "text": "Copyright 2025. ISBN 123. " + wording}],
        kind="book",
        work_scope="single_work",
    )
    chosen, _ = select(
        {"author": "Gabriel Santos", "source": "text_byline"},
        found,
        kind="book",
        work_scope="single_work",
    )
    assert chosen["author"] == "Amina Rao"


def test_copyright_alone_does_not_become_author_and_parent_claim_does_not_cross_boundary():
    found, _ = claims(
        [{"page": 5, "text": "Copyright Amina Rao 2025. All rights reserved."}],
        kind="book",
        work_scope="single_work",
    )
    assert not found
    found, _ = claims(
        [
            {
                "page": 2,
                "text": "Copyright 2025. The rights of Amina Rao as author have been asserted.",
            }
        ],
        kind="book_chapter",
        work_scope="chapter",
    )
    current = {"author": "Gabriel Santos", "source": "text_byline"}
    assert (
        select(current, found, kind="book_chapter", work_scope="chapter")[0] == current
    )


def test_conflicting_explicit_responsibility_defers_instead_of_first_wins():
    found, _ = claims(
        [
            {
                "page": 5,
                "text": "Copyright 2025. The rights of Amina Rao as author have been asserted. The rights of Gabriel Santos as author have been asserted.",
            }
        ],
        kind="book",
        work_scope="single_work",
    )
    result, reason = select(
        {"author": "Amina Rao"}, found, kind="book", work_scope="single_work"
    )
    assert not result["author"] and "conflicting" in reason


def test_role_occurrences_do_not_globally_disqualify_same_person():
    found, _ = claims(
        [
            {"page": 2, "text": "A Series Edited by Amina Rao"},
            {
                "page": 5,
                "text": "Copyright 2025. The rights of Amina Rao as author have been asserted.",
            },
        ],
        kind="book",
        work_scope="single_work",
    )
    assert {c["role"] for c in found} == {"series_editor", "author"}
    assert (
        select({}, found, kind="book", work_scope="single_work")[0]["author"]
        == "Amina Rao"
    )


def test_all_scheduled_ocr_attempted_after_success_and_failure(tmp_path, monkeypatch):
    import author_metadata.evidence_collection as acquisition

    path = tmp_path / "neutral.pdf"
    with fitz.open() as doc:
        for i in range(5):
            page = doc.new_page()
            if i >= 3:
                page.insert_text((50, 60), "Amina Rao is credited here. " * 8)
        doc.save(path)
    _CACHE.clear()
    monkeypatch.setattr(
        acquisition.shutil, "which", lambda _: str(Path(__file__).resolve())
    )
    called = []

    def ocr(*args, **kwargs):
        called.append(len(called) + 1)
        if len(called) == 2:
            raise subprocess.TimeoutExpired("tesseract", 6)
        return SimpleNamespace(stdout=b"Amina Rao")

    monkeypatch.setattr(acquisition.subprocess, "run", ocr)
    result = collect(ctx(path=path))
    trace = result.evidence_bundle["trace"]
    assert len(called) == len(trace["ocr_plan"]) == 3
    assert [a["status"] for a in trace["attempts"] if a["kind"] == "ocr"] == [
        "read",
        "failed",
        "read",
    ]
    assert trace["native_plan"] == [1, 2, 3, 4, 5]
    assert trace["complete"] is False
    _CACHE.clear()


def test_later_specialist_still_called_after_an_accepted_author(monkeypatch):
    from author_metadata import dispatcher
    from author_metadata.adjudication import infer

    called = []
    monkeypatch.setattr(
        dispatcher,
        "_infer_before_collection",
        lambda _: {"author": "Gabriel Santos", "source": "text_byline"},
    )
    monkeypatch.setattr(
        dispatcher, "biographic_titlepage", lambda _: called.append("bio") or {}
    )
    context = ctx(
        [
            {
                "page": 5,
                "text": "Copyright 2025. The rights of Amina Rao as author have been asserted.",
            }
        ]
    )
    result = infer(context)
    assert called and result["author"] == "Amina Rao"
    assert result["evidence_review"]["all_scheduled_experts_assessed"]


def test_oxford_comma_does_not_erase_a_complete_roster():
    from author_metadata.structured_credit import _names

    assert _names("Amina Rao, Gabriel Santos, and Elena Chen") == [
        "Amina Rao",
        "Gabriel Santos",
        "Elena Chen",
    ]


def test_corporate_research_role_does_not_erase_explicit_personal_byline():
    found, _ = claims(
        [
            {"page": 1, "text": "RECOMMENDED CITATION\nExample Research Center, 2025"},
            {"page": 3, "text": "Example Research Center conducted this study."},
        ],
        kind="report",
        work_scope="single_work",
    )
    assert found
    current = {"author": "Amina Rao, Gabriel Santos", "source": "text_byline"}
    assert select(current, found, kind="report", work_scope="single_work")[0] == current
    assert (
        select({}, found, kind="report", work_scope="single_work")[0]["author"]
        == "Example Research Center"
    )


def test_publisher_ownership_and_imprint_repetition_do_not_establish_personal_authorship():
    found, _ = claims(
        [
            {"page": 1, "text": "Cities and Cultural Memory\nParadigm Publishers"},
            {"page": 2, "text": "Copyright © 2025 Paradigm Publishers"},
        ],
        kind="book",
        work_scope="single_work",
    )
    assert not [c for c in found if c["role"] == "author"]


def test_whole_issue_does_not_inherit_a_local_contributor_roster():
    assertion = {
        "author": "Amina Rao, Gabriel Santos",
        "role": "author",
        "family": "local_chapter_contributors",
        "priority": 115,
    }
    assert not select(
        {}, [assertion], kind="periodical_issue", work_scope="whole_issue"
    )[0].get("author")


def test_interview_title_requires_a_questioner_role_before_selecting_one_name():
    samples = [
        {"page": 1, "text": "In Conversation\nGabriel Santos with Amina Rao"},
        {
            "page": 3,
            "text": "Amina Rao (Review): How did you construct this argument?\nGabriel Santos: I began with the archives.",
        },
    ]
    found, _ = claims(samples, kind="generic", work_scope="single_work")
    assert (
        select({}, found, kind="generic", work_scope="single_work")[0]["author"]
        == "Amina Rao"
    )
    found, _ = claims(samples[:1], kind="generic", work_scope="single_work")
    assert not select({}, found, kind="generic", work_scope="single_work")[0].get(
        "author"
    )


def test_different_corporate_responsibility_assertions_do_not_collapse_to_one_empty_person_key():
    base = {
        "role": "author",
        "family": "corporate_citation_and_responsibility",
        "priority": 104,
        "page": 1,
        "source": "text_corporate_cover_byline",
        "evidence": "citation and research act",
    }
    result, reason = select(
        {},
        [
            dict(base, author="Example Research Center"),
            dict(base, author="Other Research Institute"),
        ],
        kind="report",
        work_scope="single_work",
    )
    assert not result.get("author") and "conflicting" in reason


def test_two_later_running_heads_can_resolve_a_local_ocr_spelling_variant():
    from author_metadata.publishing_relations import running_chapter_claims

    def region(page, text):
        lines = [{"text": value, "box": [20, y, 300, y + 12]} for value, y in text]
        return {"page": page, "height": 800, "lines": lines}

    bundle = {
        "trace": {"document_page_count": 24},
        "ocr": [
            region(
                3,
                [
                    ("CHAPTER 5", 100),
                    ("Cities and Cultural Memory", 140),
                    ("Amina Reo", 185),
                ],
            ),
            region(4, [("88 Amina Rao", 25)]),
            region(5, [("90 Amina Rao", 25)]),
        ],
    }
    found = running_chapter_claims(bundle, kind="generic", work_scope="single_work")
    assert found[0]["author"] == "Amina Rao"
    assert found[0]["corroboration_pages"] == [4, 5]
    assert found[0]["local_ocr_spelling"] == "Amina Reo"
    assert (
        select(
            {"author": "Amina Reo"}, found, kind="generic", work_scope="single_work"
        )[0]["author"]
        == "Amina Rao"
    )
    bundle["ocr"].pop()
    assert not running_chapter_claims(bundle, kind="generic", work_scope="single_work")


def test_previous_chapter_header_cannot_settle_a_later_local_byline():
    from author_metadata.publishing_relations import running_chapter_claims

    bundle = {
        "ocr": [
            {
                "page": 1,
                "height": 800,
                "lines": [{"text": "Amina Rao", "box": [20, 20, 300, 30]}],
            },
            {
                "page": 2,
                "height": 800,
                "lines": [{"text": "Amina Rao", "box": [20, 20, 300, 30]}],
            },
            {
                "page": 3,
                "height": 800,
                "lines": [
                    {"text": "CHAPTER 5", "box": [20, 100, 300, 112]},
                    {"text": "Cities and Cultural Memory", "box": [20, 140, 300, 152]},
                    {"text": "Amina Reo", "box": [20, 180, 300, 192]},
                ],
            },
        ]
    }
    assert not running_chapter_claims(bundle, kind="generic", work_scope="single_work")


def test_truncated_prose_cannot_be_completed_into_a_chapter_author():
    from author_metadata.book_chapter import _names

    assert not _names("wood films, and consequently classi-")
    assert _names("Amina Francis-Rao") == ["Amina Francis-Rao"]
